package auth

import (
	"strings"
	"testing"
)

func TestGenerateKeyRoundTrip(t *testing.T) {
	plaintext, keyID, hash, err := GenerateKey()
	if err != nil {
		t.Fatalf("GenerateKey: %v", err)
	}

	if !strings.HasPrefix(plaintext, "cg_") {
		t.Errorf("plaintext = %q, want a cg_ prefix so secret scanners can spot it", plaintext)
	}
	// The stored hash must not contain the secret in any recoverable form.
	_, secret, err := SplitKey(plaintext)
	if err != nil {
		t.Fatalf("SplitKey on a generated key: %v", err)
	}
	if strings.Contains(hash, secret) {
		t.Fatal("the secret appears verbatim in the stored hash")
	}

	gotID, gotSecret, err := SplitKey(plaintext)
	if err != nil {
		t.Fatalf("SplitKey: %v", err)
	}
	if gotID != keyID {
		t.Errorf("key id = %q, want %q", gotID, keyID)
	}

	ok, err := VerifySecret(gotSecret, hash)
	if err != nil || !ok {
		t.Errorf("VerifySecret = %v, %v; want true, nil", ok, err)
	}
}

func TestGeneratedKeysAreUnique(t *testing.T) {
	seen := map[string]bool{}
	for range 20 {
		_, keyID, _, err := GenerateKey()
		if err != nil {
			t.Fatal(err)
		}
		if seen[keyID] {
			t.Fatalf("key id %q generated twice", keyID)
		}
		seen[keyID] = true
	}
}

func TestVerifySecretRejectsAWrongSecret(t *testing.T) {
	hash, err := HashSecret("correct-horse")
	if err != nil {
		t.Fatal(err)
	}
	for _, wrong := range []string{"", "correct-hors", "correct-horsee", "CORRECT-HORSE"} {
		ok, err := VerifySecret(wrong, hash)
		if err != nil {
			t.Fatalf("VerifySecret(%q): %v", wrong, err)
		}
		if ok {
			t.Errorf("VerifySecret accepted %q", wrong)
		}
	}
}

func TestHashSecretIsSaltedPerCall(t *testing.T) {
	// Equal secrets must not produce equal hashes, or the store leaks which keys
	// share a secret.
	a, err := HashSecret("same")
	if err != nil {
		t.Fatal(err)
	}
	b, err := HashSecret("same")
	if err != nil {
		t.Fatal(err)
	}
	if a == b {
		t.Fatal("two hashes of the same secret are identical — salting is broken")
	}
}

func TestSplitKeyRejectsMalformedValues(t *testing.T) {
	valid, _, _, err := GenerateKey()
	if err != nil {
		t.Fatal(err)
	}
	id, secret, _ := SplitKey(valid)

	bad := map[string]string{
		"empty":            "",
		"old dummy value":  "dev",
		"no prefix":        id + "_" + secret,
		"wrong prefix":     "sk_" + id + "_" + secret,
		"no separator":     "cg_" + id + secret,
		"short id":         "cg_" + id[:10] + "_" + secret,
		"short secret":     "cg_" + id + "_" + secret[:20],
		"uppercase hex":    "cg_" + strings.ToUpper(id) + "_" + secret,
		"non-hex id":       "cg_" + strings.Repeat("z", len(id)) + "_" + secret,
		"non-hex secret":   "cg_" + id + "_" + strings.Repeat("z", len(secret)),
		"trailing newline": valid + "\n",
		"sql-ish":          "cg_' OR 1=1--_" + secret,
	}
	for name, value := range bad {
		t.Run(name, func(t *testing.T) {
			if _, _, err := SplitKey(value); err == nil {
				t.Errorf("SplitKey(%q) accepted a malformed key", value)
			}
		})
	}
}

func TestVerifySecretRejectsUnusableHashes(t *testing.T) {
	bad := []string{
		"",
		"plaintext",
		"$2a$10$abcdefghijklmnopqrstuv", // bcrypt, not ours
		"$argon2i$v=19$m=65536,t=3,p=4$c2FsdA$aGFzaA",  // argon2i, not id
		"$argon2id$v=99$m=65536,t=3,p=4$c2FsdA$aGFzaA", // unknown version
		"$argon2id$v=19$m=65536,t=3$c2FsdA$aGFzaA",     // missing p
		"$argon2id$v=19$m=0,t=0,p=0$c2FsdA$aGFzaA",     // nonsense cost
		"$argon2id$v=19$m=65536,t=3,p=4$!!!$aGFzaA",    // salt not base64
	}
	for _, encoded := range bad {
		ok, err := VerifySecret("whatever", encoded)
		if ok {
			t.Errorf("VerifySecret accepted hash %q", encoded)
		}
		if err == nil {
			t.Errorf("VerifySecret(%q) returned no error — an unreadable hash must be reported, "+
				"not silently treated as a mismatch", encoded)
		}
	}
}

func TestVerifySecretHonoursStoredParameters(t *testing.T) {
	// Parameters are read from the stored value, not from the constants, so a key
	// hashed under a cheaper configuration keeps working after the cost is raised.
	// Otherwise raising it locks out every existing client.
	cheap := "$argon2id$v=19$m=8,t=1,p=1$" +
		"c2FsdHNhbHRzYWx0c2Ex" + "$"
	_ = cheap

	hash, err := HashSecret("s3cret")
	if err != nil {
		t.Fatal(err)
	}
	// Rewrite the encoded parameters to something cheaper and confirm the
	// verifier follows the value rather than the build-time constants: the hash
	// no longer matches, but it must be reported as a mismatch, not a parse error.
	tampered := strings.Replace(hash, "t=3", "t=1", 1)
	ok, err := VerifySecret("s3cret", tampered)
	if err != nil {
		t.Fatalf("stored parameters were not honoured: %v", err)
	}
	if ok {
		t.Error("a hash computed at different parameters must not verify")
	}

	// The untampered value still verifies.
	ok, err = VerifySecret("s3cret", hash)
	if err != nil || !ok {
		t.Fatalf("VerifySecret = %v, %v; want true, nil", ok, err)
	}
}
