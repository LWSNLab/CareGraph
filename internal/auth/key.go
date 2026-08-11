package auth

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"runtime"
	"strconv"
	"strings"

	"golang.org/x/crypto/argon2"
)

// Key layout: cg_<key_id>_<secret>
//
// Two halves, because Argon2id cannot be used for lookup. It is slow by design,
// so hashing the presented key against every stored row is not an option, and a
// per-key salt rules out indexing the hash. `keyID` selects one row; only that
// row's hash is then verified. One indexed lookup plus one verification.
const (
	keyPrefix = "cg_"
	keyIDLen  = 8  // bytes, 16 hex chars — public, indexed
	secretLen = 32 // bytes, 64 hex chars — the part that is hashed
)

// Argon2id parameters. RFC 9106's second recommended configuration (64 MiB,
// t=3), which is the usual interactive-login setting. Deliberately not raised
// further: this runs on the request path, and the verification cache below is
// what keeps it off the hot path rather than a weaker KDF.
const (
	argonTime    uint32 = 3
	argonMemory  uint32 = 64 * 1024 // KiB
	argonKeyLen  uint32 = 32
	argonSaltLen        = 16
)

var (
	// ErrMalformedKey means the value cannot be a CareGraph key at all — no
	// database lookup is attempted for it.
	ErrMalformedKey = errors.New("malformed api key")
	// ErrUnknownHash marks a stored hash this build cannot interpret.
	ErrUnknownHash = errors.New("unsupported password hash")
)

// argonThreads is capped so a container with a low CPU quota does not have the
// KDF fan out wider than it can actually run.
func argonThreads() uint8 {
	if n := runtime.GOMAXPROCS(0); n < 4 {
		return uint8(max(n, 1))
	}
	return 4
}

// GenerateKey returns a new key and its two derived parts: the plaintext to hand
// to the client (shown once, never stored), the public key id, and the Argon2id
// encoding of the secret.
func GenerateKey() (plaintext, keyID, secretHash string, err error) {
	idBytes := make([]byte, keyIDLen)
	secretBytes := make([]byte, secretLen)
	if _, err = rand.Read(idBytes); err != nil {
		return "", "", "", fmt.Errorf("generate key id: %w", err)
	}
	if _, err = rand.Read(secretBytes); err != nil {
		return "", "", "", fmt.Errorf("generate secret: %w", err)
	}

	keyID = hex.EncodeToString(idBytes)
	secret := hex.EncodeToString(secretBytes)
	plaintext = keyPrefix + keyID + "_" + secret

	secretHash, err = HashSecret(secret)
	if err != nil {
		return "", "", "", err
	}
	return plaintext, keyID, secretHash, nil
}

// SplitKey pulls the lookup id and the secret out of a presented key.
//
// Rejects anything that is not shaped like a key before the database is touched,
// so a scan with random values costs a string check rather than a query.
func SplitKey(presented string) (keyID, secret string, err error) {
	if !strings.HasPrefix(presented, keyPrefix) {
		return "", "", ErrMalformedKey
	}
	body := presented[len(keyPrefix):]
	id, sec, found := strings.Cut(body, "_")
	if !found || len(id) != keyIDLen*2 || len(sec) != secretLen*2 {
		return "", "", ErrMalformedKey
	}
	if !isHex(id) || !isHex(sec) {
		return "", "", ErrMalformedKey
	}
	return id, sec, nil
}

func isHex(s string) bool {
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !(c >= '0' && c <= '9' || c >= 'a' && c <= 'f') {
			return false
		}
	}
	return len(s) > 0
}

// HashSecret returns the standard encoded Argon2id form, so the parameters
// travel with the value and can be raised later without a migration.
func HashSecret(secret string) (string, error) {
	salt := make([]byte, argonSaltLen)
	if _, err := rand.Read(salt); err != nil {
		return "", fmt.Errorf("generate salt: %w", err)
	}
	threads := argonThreads()
	sum := argon2.IDKey([]byte(secret), salt, argonTime, argonMemory, threads, argonKeyLen)

	return fmt.Sprintf(
		"$argon2id$v=%d$m=%d,t=%d,p=%d$%s$%s",
		argon2.Version, argonMemory, argonTime, threads,
		base64.RawStdEncoding.EncodeToString(salt),
		base64.RawStdEncoding.EncodeToString(sum),
	), nil
}

// VerifySecret checks a secret against an encoded Argon2id hash.
//
// Parameters are read from the stored value rather than from the constants
// above, so keys hashed under an older configuration keep working after the
// cost is raised.
func VerifySecret(secret, encoded string) (bool, error) {
	parts := strings.Split(encoded, "$")
	// ["", "argon2id", "v=19", "m=...,t=...,p=...", salt, hash]
	if len(parts) != 6 || parts[1] != "argon2id" {
		return false, ErrUnknownHash
	}

	var version int
	if _, err := fmt.Sscanf(parts[2], "v=%d", &version); err != nil || version != argon2.Version {
		return false, ErrUnknownHash
	}

	memory, time, threads, err := parseArgonParams(parts[3])
	if err != nil {
		return false, err
	}

	salt, err := base64.RawStdEncoding.DecodeString(parts[4])
	if err != nil {
		return false, ErrUnknownHash
	}
	want, err := base64.RawStdEncoding.DecodeString(parts[5])
	if err != nil {
		return false, ErrUnknownHash
	}

	got := argon2.IDKey([]byte(secret), salt, time, memory, threads, uint32(len(want)))
	// Constant time: a byte-by-byte comparison leaks how much of a guess was
	// right, which is enough to reconstruct a hash one byte at a time.
	return subtle.ConstantTimeCompare(got, want) == 1, nil
}

func parseArgonParams(s string) (memory, time uint32, threads uint8, err error) {
	for _, field := range strings.Split(s, ",") {
		name, value, ok := strings.Cut(field, "=")
		if !ok {
			return 0, 0, 0, ErrUnknownHash
		}
		n, convErr := strconv.ParseUint(value, 10, 32)
		if convErr != nil {
			return 0, 0, 0, ErrUnknownHash
		}
		switch name {
		case "m":
			memory = uint32(n)
		case "t":
			time = uint32(n)
		case "p":
			if n == 0 || n > 255 {
				return 0, 0, 0, ErrUnknownHash
			}
			threads = uint8(n)
		default:
			return 0, 0, 0, ErrUnknownHash
		}
	}
	if memory == 0 || time == 0 || threads == 0 {
		return 0, 0, 0, ErrUnknownHash
	}
	return memory, time, threads, nil
}

// cacheKey is the in-memory cache's index for a presented key. SHA-256 rather
// than the key itself so a heap dump does not hand out working credentials.
func cacheKey(presented string) [32]byte {
	return sha256.Sum256([]byte(presented))
}
