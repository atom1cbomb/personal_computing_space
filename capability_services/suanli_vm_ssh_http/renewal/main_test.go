package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/openpubkey/openpubkey/client"
	opkoidc "github.com/openpubkey/openpubkey/oidc"
	"github.com/openpubkey/openpubkey/pktoken"
	"github.com/openpubkey/openpubkey/providers"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"golang.org/x/crypto/ssh"
)

func testCredential(t *testing.T) *credential {
	t.Helper()
	p, _, _, err := providers.NewMockProvider(providers.DefaultMockProviderOpts())
	if err != nil {
		t.Fatal(err)
	}
	opk, err := client.New(p)
	if err != nil {
		t.Fatal(err)
	}
	pk, err := opk.Auth(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	key, err := ssh.MarshalPrivateKey(opk.GetSigner(), "openpubkey cert")
	if err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(pk)
	if err != nil {
		t.Fatal(err)
	}
	return &credential{ConfigHash: "test-config", PrivateKey: pem.EncodeToMemory(key),
		PKToken: raw, OriginalIDToken: pk.OpToken, RefreshToken: []byte("private-refresh-old"),
		ExpiresAt: time.Now().Add(time.Hour).Unix()}
}

type fakeRefresher struct {
	tokens    *opkoidc.Tokens
	err       error
	verifyErr error
	received  []byte
	verified  bool
}

func (f *fakeRefresher) RefreshTokens(_ context.Context, token []byte) (*opkoidc.Tokens, error) {
	f.received = token
	return f.tokens, f.err
}
func (f *fakeRefresher) VerifyRefreshedIDToken(_ context.Context, _, _ []byte) error {
	f.verified = true
	return f.verifyErr
}
func futureToken() []byte {
	payload := fmt.Sprintf(`{"exp":%d}`, time.Now().Add(2*time.Hour).Unix())
	return []byte("e30." + base64.RawURLEncoding.EncodeToString([]byte(payload)) + ".c2ln")
}

func TestRefreshPersistsRotatedTokenAndFreshCertificateAcrossRestart(t *testing.T) {
	root := t.TempDir()
	key := filepath.Join(root, ".ssh", "id_ecdsa")
	original := testCredential(t)
	if err := save(root, key, original); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(key + "-cert.pub")
	p := &fakeRefresher{tokens: &opkoidc.Tokens{IDToken: futureToken(), RefreshToken: []byte("private-refresh-new")}}
	updated, err := refresh(context.Background(), p, original)
	if err != nil {
		t.Fatal(err)
	}
	if !p.verified || !bytes.Equal(p.received, original.RefreshToken) {
		t.Fatal("refresh not verified")
	}
	if err = save(root, key, updated); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "renewal.json"))
	var restarted credential
	if err = json.Unmarshal(raw, &restarted); err != nil {
		t.Fatal(err)
	}
	if string(restarted.RefreshToken) != "private-refresh-new" || !bytes.Equal(restarted.FreshIDToken, p.tokens.IDToken) {
		t.Fatal("rotated credentials lost on restart")
	}
	if err = writeKeys(key, &restarted); err != nil {
		t.Fatal(err)
	}
	after, _ := os.ReadFile(key + "-cert.pub")
	if bytes.Equal(before, after) {
		t.Fatal("refreshed ID token not written to certificate")
	}
	pub, _, _, _, err := ssh.ParseAuthorizedKey(after)
	if err != nil {
		t.Fatal(err)
	}
	cert, ok := pub.(*ssh.Certificate)
	if !ok {
		t.Fatal("not an SSH certificate")
	}
	pk, err := pktoken.NewFromCompact([]byte(cert.Extensions["openpubkey-pkt"]))
	if err != nil || !bytes.Equal(pk.FreshIDToken, p.tokens.IDToken) {
		t.Fatal("certificate lost refreshed ID token", err)
	}
	for _, path := range []string{key, key + "-cert.pub", filepath.Join(root, "renewal.json")} {
		info, _ := os.Stat(path)
		if info.Mode().Perm() != 0600 {
			t.Fatal("credential permissions", info.Mode())
		}
	}
	// Providers may keep using the same refresh token.
	p.tokens.RefreshToken = nil
	again, err := refresh(context.Background(), p, &restarted)
	if err != nil || !bytes.Equal(again.RefreshToken, restarted.RefreshToken) {
		t.Fatal("refresh token not retained", err)
	}
}

func TestFailedRefreshPreservesCredentialAndRejectsIdentityMismatch(t *testing.T) {
	c := testCredential(t)
	before, _ := json.Marshal(c)
	for _, p := range []*fakeRefresher{
		{err: errors.New("temporary network error")},
		{err: oidc.ErrInvalidGrant()},
		{tokens: &opkoidc.Tokens{IDToken: futureToken()}, verifyErr: errors.New("subject mismatch")},
	} {
		if _, err := refresh(context.Background(), p, c); err == nil {
			t.Fatal("failure accepted")
		}
		after, _ := json.Marshal(c)
		if !bytes.Equal(before, after) {
			t.Fatal("failure destroyed credential")
		}
	}
	if !revoked(fmt.Errorf("wrapped: %w", oidc.ErrInvalidGrant())) {
		t.Fatal("revocation not recognized")
	}
	if revoked(errors.New("network error invalid_grant text")) || revoked(oidc.ErrInvalidClient()) {
		t.Fatal("incorrect login prompt")
	}
}

func TestCancellationStopsWaitAndStatusContainsNoCredentials(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if wait(ctx, time.Hour) {
		t.Fatal("cancel ignored")
	}
	root := t.TempDir()
	if err := setStatus(root, "auth_failed"); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "renewal-status.json"))
	if string(raw) != `{"status":"auth_failed"}` {
		t.Fatal("unexpected public status")
	}
}

type recoveringRefresher struct {
	fakeRefresher
	calls int
}

func (p *recoveringRefresher) RefreshTokens(ctx context.Context, token []byte) (*opkoidc.Tokens, error) {
	p.calls++
	if p.calls == 1 {
		return nil, errors.New("temporary network outage")
	}
	return p.fakeRefresher.RefreshTokens(ctx, token)
}

func TestRenewalLoopRecoversWithoutNewLogin(t *testing.T) {
	root := t.TempDir()
	key := filepath.Join(root, "id_ecdsa")
	c := testCredential(t)
	c.ExpiresAt = time.Now().Unix()
	if err := save(root, key, c); err != nil {
		t.Fatal(err)
	}
	p := &recoveringRefresher{fakeRefresher: fakeRefresher{tokens: &opkoidc.Tokens{IDToken: futureToken(), RefreshToken: []byte("rotated")}}}
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- renew(ctx, p, c, root, key) }()
	sawFailure, recovered := false, false
	for ctx.Err() == nil {
		raw, _ := os.ReadFile(filepath.Join(root, "renewal-status.json"))
		if bytes.Contains(raw, []byte("auth_failed")) {
			sawFailure = true
		}
		state, _ := os.ReadFile(filepath.Join(root, "renewal.json"))
		var current credential
		_ = json.Unmarshal(state, &current)
		if sawFailure && bytes.Contains(raw, []byte("authenticated")) && string(current.RefreshToken) == "rotated" {
			recovered = true
			cancel()
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if !recovered || p.calls != 2 {
		t.Fatal("did not recover with bounded retry", p.calls)
	}
}

func TestRevocationStopsLoopAndResumeRestoresPrivateState(t *testing.T) {
	root := t.TempDir()
	key := filepath.Join(root, "id_ecdsa")
	c := testCredential(t)
	c.ExpiresAt = time.Now().Unix()
	if err := save(root, key, c); err != nil {
		t.Fatal(err)
	}
	if err := renew(context.Background(), &fakeRefresher{err: oidc.ErrInvalidGrant()}, c, root, key); err != nil {
		t.Fatal(err)
	}
	raw, _ := os.ReadFile(filepath.Join(root, "renewal-status.json"))
	if string(raw) != `{"status":"auth_required"}` {
		t.Fatal("revocation status", string(raw))
	}
	// Restart a still-authorized session without a browser or network call.
	config := filepath.Join(root, "config.yml")
	if err := os.WriteFile(config, []byte("default_provider: test\nproviders:\n  - alias: test\n    issuer: https://idp.invalid\n    client_id: test\n    scopes: openid offline_access\n"), 0600); err != nil {
		t.Fatal(err)
	}
	_, hash, err := newProvider(config)
	if err != nil {
		t.Fatal(err)
	}
	c.ConfigHash, c.ExpiresAt = hash, time.Now().Add(time.Hour).Unix()
	if err = save(root, key, c); err != nil {
		t.Fatal(err)
	}
	_ = os.Remove(key)
	_ = os.Remove(key + "-cert.pub")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err = run(ctx, "resume", config, root, key); err != nil {
		t.Fatal(err)
	}
	if _, err = os.Stat(key + "-cert.pub"); err != nil {
		t.Fatal("restart did not restore key", err)
	}
	c.ConfigHash = "another-provider"
	if err = save(root, key, c); err != nil {
		t.Fatal(err)
	}
	if run(ctx, "resume", config, root, key) == nil {
		t.Fatal("wrong provider state accepted")
	}
}
