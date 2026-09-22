# Local browser bridge

Run the entrypoint with the `client` object returned by Observer's
`auth/start` response:

```bash
python client/loom_vm_auth.py \
  --bridge-url 'https://observer.example/api/v1/auth/browser/bridge' \
  --session-id '<session id>' \
  --session-token '<short-lived token>'
```

Then open the printed `http://localhost:3000/login?...` URL. The token is
single-session and expires after five minutes; do not save it in a config file.
The process only handles `/login`, `/login-callback`, and `/result` on local
loopback. It does not need SSH access to the Observer host.
