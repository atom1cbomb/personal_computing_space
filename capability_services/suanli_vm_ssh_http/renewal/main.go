// Activation-local login and durable renewal using the official OpenPubkey
// protocol and opkssh certificate implementation. No credentials go to stdout.
package main

import (
	"context"
	"crypto"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/openpubkey/openpubkey/client"
	opkoidc "github.com/openpubkey/openpubkey/oidc"
	"github.com/openpubkey/openpubkey/pktoken"
	"github.com/openpubkey/openpubkey/pktoken/clientinstance"
	"github.com/openpubkey/openpubkey/providers"
	"github.com/openpubkey/opkssh/sshcert"
	"github.com/zitadel/oidc/v3/pkg/oidc"
	"golang.org/x/crypto/ssh"
	"gopkg.in/yaml.v3"
)

type configuration struct {
	Default   string `yaml:"default_provider"`
	Providers []struct {
		Alias      string   `yaml:"alias"`
		Issuer     string   `yaml:"issuer"`
		ClientID   string   `yaml:"client_id"`
		Scopes     string   `yaml:"scopes"`
		AccessType string   `yaml:"access_type"`
		Prompt     string   `yaml:"prompt"`
		Redirects  []string `yaml:"redirect_uris"`
	} `yaml:"providers"`
}

type capturingProvider struct {
	*providers.StandardOpRefreshable
	tokens *opkoidc.Tokens
}

func (p *capturingProvider) RequestTokens(ctx context.Context, claims *clientinstance.Claims) (*opkoidc.Tokens, error) {
	tokens, err := p.StandardOpRefreshable.RequestTokens(ctx, claims)
	if err == nil {
		p.tokens = tokens
	}
	return tokens, err
}

type credential struct {
	ConfigHash      string          `json:"config_hash"`
	PrivateKey      []byte          `json:"private_key"`
	PKToken         json.RawMessage `json:"pk_token"`
	OriginalIDToken []byte          `json:"original_id_token"`
	FreshIDToken    []byte          `json:"fresh_id_token,omitempty"`
	RefreshToken    []byte          `json:"refresh_token"`
	ExpiresAt       int64           `json:"expires_at"`
}

func atomicWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".renewal-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if _, err = file.Write(data); err == nil {
		err = file.Sync()
	}
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(file.Name(), path)
}

func setStatus(root, status string) error {
	raw, _ := json.Marshal(map[string]string{"status": status})
	return atomicWrite(filepath.Join(root, "renewal-status.json"), raw)
}

func expiration(token []byte) (int64, error) {
	parts := strings.Split(string(token), ".")
	if len(parts) != 3 {
		return 0, errors.New("invalid ID token")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return 0, err
	}
	var claims struct {
		Exp int64 `json:"exp"`
	}
	if err := json.Unmarshal(raw, &claims); err != nil || claims.Exp <= 0 {
		return 0, errors.New("missing token expiration")
	}
	return claims.Exp, nil
}

func newProvider(path string) (*capturingProvider, string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, "", err
	}
	hash := sha256.Sum256(raw)
	var config configuration
	if err = yaml.Unmarshal(raw, &config); err != nil {
		return nil, "", err
	}
	for _, p := range config.Providers {
		if p.Alias != config.Default {
			continue
		}
		opts := providers.GetDefaultStandardOpOptions(p.Issuer, p.ClientID)
		opts.Scopes = strings.Fields(p.Scopes)
		opts.RedirectURIs = p.Redirects
		opts.PromptType, opts.AccessType = p.Prompt, p.AccessType
		opts.OpenBrowser = false
		opts.HttpClient = &http.Client{Timeout: 20 * time.Second}
		standard := providers.NewStandardOpWithOptions(opts).(*providers.StandardOp)
		return &capturingProvider{StandardOpRefreshable: &providers.StandardOpRefreshable{StandardOp: *standard}}, hex.EncodeToString(hash[:]), nil
	}
	return nil, "", errors.New("provider not configured")
}

func login(ctx context.Context, provider *capturingProvider, hash string) (*credential, error) {
	opk, err := client.New(provider)
	if err != nil {
		return nil, err
	}
	pk, err := opk.Auth(ctx)
	if err != nil {
		return nil, err
	}
	if provider.tokens == nil || len(provider.tokens.RefreshToken) == 0 {
		return nil, errors.New("provider did not issue a refresh token")
	}
	key, err := ssh.MarshalPrivateKey(opk.GetSigner(), "openpubkey cert")
	if err != nil {
		return nil, err
	}
	rawPK, err := json.Marshal(pk)
	if err != nil {
		return nil, err
	}
	exp, err := expiration(provider.tokens.IDToken)
	if err != nil {
		return nil, err
	}
	return &credential{ConfigHash: hash, PrivateKey: pem.EncodeToMemory(key), PKToken: rawPK,
		OriginalIDToken: provider.tokens.IDToken, RefreshToken: provider.tokens.RefreshToken, ExpiresAt: exp}, nil
}

func save(root, keyPath string, c *credential) error {
	// Checkpoint rotated tokens before publishing the new certificate. A restart
	// can reconstruct keys from the checkpoint without using the old refresh token.
	raw, err := json.Marshal(c)
	if err != nil {
		return err
	}
	if err = atomicWrite(filepath.Join(root, "renewal.json"), raw); err != nil {
		return err
	}
	return writeKeys(keyPath, c)
}

func writeKeys(keyPath string, c *credential) error {
	var pk pktoken.PKToken
	if err := json.Unmarshal(c.PKToken, &pk); err != nil {
		return err
	}
	// PKToken.MarshalJSON intentionally omits the refreshed ID token. Restore
	// it explicitly before building the opkssh certificate after a restart.
	pk.FreshIDToken = c.FreshIDToken
	key, err := ssh.ParseRawPrivateKey(c.PrivateKey)
	if err != nil {
		return err
	}
	signer, ok := key.(crypto.Signer)
	if !ok {
		return errors.New("invalid private key")
	}
	cert, err := sshcert.New(&pk, nil, []string{"opkssh-wildcard"})
	if err != nil {
		return err
	}
	sshSigner, err := ssh.NewSignerFromSigner(signer)
	if err != nil {
		return err
	}
	algorithmSigner, ok := sshSigner.(ssh.AlgorithmSigner)
	if !ok {
		return errors.New("unsupported SSH signer")
	}
	multiSigner, err := ssh.NewSignerWithAlgorithms(algorithmSigner, []string{sshSigner.PublicKey().Type()})
	if err != nil {
		return err
	}
	sshCert, err := cert.SignCert(multiSigner)
	if err != nil {
		return err
	}
	if err = atomicWrite(keyPath, c.PrivateKey); err != nil {
		return err
	}
	return atomicWrite(keyPath+"-cert.pub", ssh.MarshalAuthorizedKey(sshCert))
}

type refresher interface {
	RefreshTokens(context.Context, []byte) (*opkoidc.Tokens, error)
	VerifyRefreshedIDToken(context.Context, []byte, []byte) error
}

func refresh(ctx context.Context, p refresher, c *credential) (*credential, error) {
	tokens, err := p.RefreshTokens(ctx, c.RefreshToken)
	if err != nil {
		return nil, err
	}
	if err = p.VerifyRefreshedIDToken(ctx, c.OriginalIDToken, tokens.IDToken); err != nil {
		return nil, err
	}
	exp, err := expiration(tokens.IDToken)
	if err != nil {
		return nil, err
	}
	if exp <= time.Now().Unix() {
		return nil, errors.New("expired refresh result")
	}
	updated := *c
	updated.FreshIDToken, updated.ExpiresAt = tokens.IDToken, exp
	if len(tokens.RefreshToken) > 0 {
		updated.RefreshToken = tokens.RefreshToken
	}
	return &updated, nil
}

func revoked(err error) bool {
	var oauthErr *oidc.Error
	return errors.As(err, &oauthErr) && oauthErr.ErrorType == "invalid_grant"
}

func wait(ctx context.Context, delay time.Duration) bool {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func run(ctx context.Context, mode, config, root, keyPath string) error {
	p, hash, err := newProvider(config)
	if err != nil {
		return err
	}
	var c *credential
	if mode == "login" {
		if c, err = login(ctx, p, hash); err != nil {
			return err
		}
		if err = save(root, keyPath, c); err != nil {
			return err
		}
	} else {
		raw, err := os.ReadFile(filepath.Join(root, "renewal.json"))
		if err != nil {
			return err
		}
		if err = json.Unmarshal(raw, &c); err != nil {
			return err
		}
		if c == nil || c.ConfigHash != hash || len(c.RefreshToken) == 0 {
			return errors.New("invalid renewal state")
		}
		if err = writeKeys(keyPath, c); err != nil {
			return err
		}
	}
	return renew(ctx, p, c, root, keyPath)
}

func renew(ctx context.Context, p refresher, c *credential, root, keyPath string) error {
	status := "authenticated"
	if c.ExpiresAt <= time.Now().Unix() {
		status = "auth_failed"
	}
	if err := setStatus(root, status); err != nil {
		return err
	}
	for {
		delay := time.Until(time.Unix(c.ExpiresAt, 0)) - time.Minute
		if !wait(ctx, max(delay, time.Second)) {
			return nil
		}
		backoff := 2 * time.Second
		for {
			attempt, cancel := context.WithTimeout(ctx, 30*time.Second)
			updated, refreshErr := refresh(attempt, p, c)
			cancel()
			if ctx.Err() != nil {
				return nil
			}
			if refreshErr == nil {
				// Keep the newly rotated token in memory even if disk is temporarily
				// unavailable; retry persistence before any further refresh request.
				c = updated
				for save(root, keyPath, c) != nil {
					_ = setStatus(root, "auth_failed")
					if !wait(ctx, backoff) {
						return nil
					}
					backoff = min(backoff*2, time.Minute)
				}
				if err := setStatus(root, "authenticated"); err != nil {
					return err
				}
				break
			}
			if revoked(refreshErr) {
				return setStatus(root, "auth_required")
			}
			_ = setStatus(root, "auth_failed")
			if !wait(ctx, backoff) {
				return nil
			}
			backoff = min(backoff*2, time.Minute)
		}
	}
}

func main() {
	log.SetOutput(io.Discard)
	flags := flag.NewFlagSet("loom-opkssh", flag.ContinueOnError)
	flags.SetOutput(io.Discard)
	config := flags.String("config-path", "", "provider config")
	root := flags.String("state-dir", "", "private activation state")
	key := flags.String("private-key-file", "", "SSH private key path")
	if len(os.Args) < 2 || (os.Args[1] != "login" && os.Args[1] != "resume") || flags.Parse(os.Args[2:]) != nil || *root == "" || *key == "" || *config == "" {
		os.Exit(2)
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	if err := run(ctx, os.Args[1], *config, *root, *key); err != nil {
		_ = setStatus(*root, "auth_failed")
		fmt.Fprintln(os.Stderr, "SSH credential renewal failed")
		os.Exit(1)
	}
}
