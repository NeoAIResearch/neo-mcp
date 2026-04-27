# Documentation update — docs.heyneo.com/neo-mcp

A single change. The remainder of the page is current and requires no edits.

---

## Add a PEP 668 fallback to the install instructions

The install command on the page is currently:

```bash
pip install neo-mcp
```

This command fails on Ubuntu 24.04+, Debian 12+, Fedora 38+, and the majority of cloud and Docker base images. These distributions enforce [PEP 668](https://peps.python.org/pep-0668/) and reject system-wide `pip install` invocations with `error: externally-managed-environment`.

Retain the existing command as the primary instruction, and place the following note immediately beneath it:

> **Linux servers — Ubuntu 24+, Debian 12+, Fedora 38+, most cloud and Docker base images**
>
> If the install fails with `error: externally-managed-environment`, run:
>
> ```bash
> python3 -m pip install --user --break-system-packages neo-mcp
> ```
>
> `--user` confines the install to `~/.local/` — no `sudo` required and no system files are modified. `--break-system-packages` is the standard PEP 668 opt-out flag and is a no-op on macOS and Windows.
