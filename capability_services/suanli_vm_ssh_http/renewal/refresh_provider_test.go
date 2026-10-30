package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/go-jose/go-jose/v4"
	"github.com/go-jose/go-jose/v4/jwt"
	opkoidc "github.com/openpubkey/openpubkey/oidc"
	"github.com/zitadel/oidc/v3/pkg/oidc"
)

func TestRealProviderRefreshAcceptsOriginalNonceAndValidatesIdentity(t *testing.T) {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	signer, err := jose.NewSigner(jose.SigningKey{Algorithm: jose.RS256, Key: key}, (&jose.SignerOptions{}).WithHeader("kid", "test"))
	if err != nil {
		t.Fatal(err)
	}
	var server *httptest.Server
	var refreshed string
	server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		switch r.URL.Path {
		case "/.well-known/openid-configuration":
			json.NewEncoder(w).Encode(map[string]any{"issuer": server.URL, "token_endpoint": server.URL + "/token", "jwks_uri": server.URL + "/keys", "id_token_signing_alg_values_supported": []string{"RS256"}})
		case "/keys":
			json.NewEncoder(w).Encode(jose.JSONWebKeySet{Keys: []jose.JSONWebKey{{Key: &key.PublicKey, KeyID: "test", Algorithm: "RS256", Use: "sig"}}})
		case "/token":
			r.ParseForm()
			if r.Form.Get("refresh_token") != "old" || r.Form.Get("client_id") != "test" || r.Form.Get("grant_type") != "refresh_token" {
				t.Error("incorrect refresh request")
				w.WriteHeader(400)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"access_token": "access", "token_type": "Bearer", "id_token": refreshed, "refresh_token": "rotated", "expires_in": 3600})
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()
	sign := func(nonce, subject, audience string, expires int64) []byte {
		t.Helper()
		hash, err := oidc.ClaimHash("access", jose.RS256)
		if err != nil {
			t.Fatal(err)
		}
		token, err := jwt.Signed(signer).Claims(map[string]any{"at_hash": hash, "iss": server.URL, "aud": audience, "sub": subject, "nonce": nonce, "iat": time.Now().Add(-time.Minute).Unix(), "exp": expires}).Serialize()
		if err != nil {
			t.Fatal(err)
		}
		return []byte(token)
	}
	config := filepath.Join(t.TempDir(), "config.yml")
	os.WriteFile(config, []byte("default_provider: test\nproviders:\n  - alias: test\n    issuer: "+server.URL+"\n    client_id: test\n    scopes: openid offline_access\n"), 0600)
	p, _, err := newProvider(config)
	if err != nil {
		t.Fatal(err)
	}
	exp := time.Now().Add(time.Hour).Unix()
	original := sign("original-nonce", "subject", "test", exp)
	refreshed = string(sign("original-nonce", "subject", "test", exp))
	tokens, err := p.RefreshTokens(context.Background(), []byte("old"))
	if err != nil {
		t.Fatal(err)
	}
	if string(tokens.RefreshToken) != "rotated" {
		t.Fatal("rotation lost")
	}
	if err = p.StandardOpRefreshable.VerifyRefreshedIDToken(context.Background(), original, tokens.IDToken); !errors.Is(err, oidc.ErrNonceInvalid) {
		t.Fatalf("expected upstream nonce regression, got %v", err)
	}
	for _, tc := range []struct {
		name, nonce, subject, aud string
		expiry                    int64
		valid                     bool
	}{
		{"original nonce", "original-nonce", "subject", "test", exp, true},
		{"omitted nonce", "", "subject", "test", exp, true},
		{"different nonce", "different", "subject", "test", exp, false},
		{"different subject", "original-nonce", "other", "test", exp, false},
		{"different audience", "original-nonce", "subject", "other", exp, false},
		{"expired", "original-nonce", "subject", "test", time.Now().Add(-time.Minute).Unix(), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tokens.IDToken = sign(tc.nonce, tc.subject, tc.aud, tc.expiry)
			err := p.VerifyRefreshedTokens(context.Background(), original, tokens)
			if (err == nil) != tc.valid {
				t.Fatalf("valid=%v err=%v", tc.valid, err)
			}
		})
	}

	tokens.IDToken = sign("original-nonce", "subject", "test", exp)
	tokens.AccessToken = []byte("wrong-access-token")
	if err := p.VerifyRefreshedTokens(context.Background(), original, tokens); !errors.Is(err, oidc.ErrAtHash) {
		t.Fatalf("access token hash not checked: %v", err)
	}
	tokens.AccessToken = []byte("access")
	parts := strings.Split(string(tokens.IDToken), ".")
	parts[2] = "AAAA"
	tokens.IDToken = []byte(strings.Join(parts, "."))
	if err := p.VerifyRefreshedTokens(context.Background(), original, tokens); err == nil {
		t.Fatal("bad signature accepted")
	}

}

func TestPendingRotationSurvivesValidationFailureAndRestart(t *testing.T) {
	root := t.TempDir()
	key := filepath.Join(root, "id_ecdsa")
	c := testCredential(t)
	c.ExpiresAt = time.Now().Unix()
	p := &fakeRefresher{tokens: &opkoidc.Tokens{IDToken: futureToken(), RefreshToken: []byte("rotated-private")}, verifyErr: oidc.ErrNonceInvalid}
	ctx, cancel := context.WithTimeout(context.Background(), 1600*time.Millisecond)
	defer cancel()
	if err := renew(ctx, p, c, root, key, 0); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "renewal.json"))
	var restored credential
	if err := json.Unmarshal(raw, &restored); err != nil {
		t.Fatal(err)
	}
	if restored.PendingTokens == nil || string(restored.PendingTokens.RefreshToken) != "rotated-private" {
		t.Fatal("pending rotation not durable")
	}
	if restored.FreshIDToken != nil {
		t.Fatal("unverified credentials published")
	}
	status, _ := os.ReadFile(filepath.Join(root, "renewal-status.json"))
	if bytes.Contains(status, []byte("rotated-private")) || !bytes.Contains(status, []byte("nonce_mismatch")) {
		t.Fatal("diagnostic missing or leaked secret")
	}
	// A restart validates the saved response without exchanging the consumed token.
	p = &fakeRefresher{err: errors.New("must not exchange again")}
	ctx, cancel2 := context.WithTimeout(context.Background(), 1600*time.Millisecond)
	defer cancel2()
	if err := renew(ctx, p, &restored, root, key, 0); err != nil {
		t.Fatal(err)
	}
	raw, _ = os.ReadFile(filepath.Join(root, "renewal.json"))
	restored = credential{}
	json.Unmarshal(raw, &restored)
	if string(restored.RefreshToken) != "rotated-private" || restored.PendingTokens != nil || p.received != nil {
		t.Fatal("restart replayed rotation or lost checkpoint")
	}
}

func TestDiagnosticsNeverIncludeProviderErrorDescription(t *testing.T) {
	root := t.TempDir()
	err := oidc.ErrInvalidGrant().WithDescription("private-token opaque-secret")
	if err := reportStatus(root, "auth_required", "exchange", err); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "renewal-status.json"))
	if bytes.Contains(raw, []byte("private-token")) || !bytes.Contains(raw, []byte("invalid_grant")) {
		t.Fatal("unsafe diagnostic", string(raw))
	}
}

func TestSoakIntervalRotatesRepeatedlyBeforeTokenExpiry(t *testing.T) {
	root := t.TempDir()
	key := filepath.Join(root, "id_ecdsa")
	c := testCredential(t)
	p := &fakeRefresher{tokens: &opkoidc.Tokens{IDToken: futureToken(), RefreshToken: []byte("rotated")}}
	ctx, cancel := context.WithTimeout(context.Background(), 2500*time.Millisecond)
	defer cancel()
	if err := renew(ctx, p, c, root, key, time.Second); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "renewal.json"))
	var saved credential
	if err := json.Unmarshal(raw, &saved); err != nil {
		t.Fatal(err)
	}
	if saved.RenewalCount < 2 || saved.ExpiresAt <= time.Now().Unix() {
		t.Fatal("soak did not refresh a still-valid token repeatedly")
	}
}
