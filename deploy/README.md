# Production deployment

`.github/workflows/deploy.yml` tests every commit pushed to `main`, then asks
the production host to deploy that exact commit. The SSH key used by Actions
is restricted in `authorized_keys` to the forced command
`~/.local/bin/personal-agent-deploy`; it cannot open an interactive shell.

The repository needs these Actions secrets:

- `DEPLOY_SSH_HOST`
- `DEPLOY_SSH_PORT`
- `DEPLOY_SSH_USER`
- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_SSH_KNOWN_HOSTS`

The remote runner updates `/home/ubuntu/project/personal-agent`, installs the
package into its existing `.venv`, and uses `assistant reboot` to gracefully
restart the daemon and verify `/healthz`.

Deployments are intentionally fast-forward only. If the server checkout has
tracked changes or commits that are not present in the tested `main` commit,
the runner exits without changing the checkout. Reconcile or publish those
changes first; do not bypass this guard with a force reset.
