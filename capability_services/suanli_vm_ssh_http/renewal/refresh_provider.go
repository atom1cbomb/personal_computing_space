package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"

	opkoidc "github.com/openpubkey/openpubkey/oidc"
	oidcclient "github.com/zitadel/oidc/v3/pkg/client"
	"github.com/zitadel/oidc/v3/pkg/client/rp"
	"github.com/zitadel/oidc/v3/pkg/oidc"
)

// Keep exchange separate from validation: a successful exchange can consume a
// rotating refresh token even when subsequent signature/JWKS validation fails.
func (p *capturingProvider) RefreshTokens(ctx context.Context, refreshToken []byte) (*opkoidc.Tokens, error) {
	r, err := p.relyingParty(ctx)
	if err != nil {
		return nil, err
	}
	token, err := oidcclient.CallTokenEndpoint(ctx, rp.RefreshTokenRequest{
		RefreshToken: string(refreshToken), ClientID: p.ClientID(),
		ClientSecret: p.ClientSecret, GrantType: oidc.GrantTypeRefreshToken,
	}, tokenCaller{r})
	if err != nil {
		return nil, err
	}
	id, _ := token.Extra("id_token").(string)
	return &opkoidc.Tokens{IDToken: []byte(id), RefreshToken: []byte(token.RefreshToken), AccessToken: []byte(token.AccessToken)}, nil
}

type tokenCaller struct{ rp.RelyingParty }

func (r tokenCaller) TokenEndpoint() string { return r.OAuthConfig().Endpoint.TokenURL }

func (p *capturingProvider) relyingParty(ctx context.Context) (rp.RelyingParty, error) {
	return rp.NewRelyingPartyOIDC(ctx, p.Issuer(), p.ClientID(), p.ClientSecret, "", p.Scopes,
		rp.WithHTTPClient(p.HttpClient), rp.WithSigningAlgsFromDiscovery(),
		rp.WithVerifierOpts(rp.WithNonce(nil)))
}

func tokenNonce(token []byte) (string, error) {
	parts := strings.Split(string(token), ".")
	if len(parts) != 3 {
		return "", errors.New("invalid ID token")
	}
	raw, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return "", err
	}
	var claims struct {
		Nonce string `json:"nonce"`
	}
	err = json.Unmarshal(raw, &claims)
	return claims.Nonce, err
}

func (p *capturingProvider) VerifyRefreshedTokens(ctx context.Context, original []byte, tokens *opkoidc.Tokens) error {
	r, err := p.relyingParty(ctx)
	if err != nil {
		return err
	}
	// Keep signature, issuer, audience, expiry and access-token hash validation.
	claims, err := rp.VerifyTokens[*oidc.IDTokenClaims](ctx, string(tokens.AccessToken), string(tokens.IDToken), r.IDTokenVerifier())
	if err != nil {
		return err
	}
	if err = opkoidc.SameIdentity(original, tokens.IDToken); err != nil {
		return err
	}
	if err = opkoidc.RequireOlder(original, tokens.IDToken); err != nil {
		return err
	}
	nonce, err := tokenNonce(original)
	if err != nil {
		return err
	}
	// OIDC Core 12.2 permits omission; when present it must match the original.
	if claims.Nonce != "" && claims.Nonce != nonce {
		return oidc.ErrNonceInvalid
	}
	return nil
}
