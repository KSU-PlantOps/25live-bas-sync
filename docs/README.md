[← Back to the project](../README.md)

# Documentation

### Getting started

| Page | What's in it |
|---|---|
| [**How it works**](how-it-works.md) | The pipeline, why BACnet, what it writes, and how finely each building can be scheduled. |
| [**Running with Docker**](docker.md) | The recommended way to run it: quick start, the config folder, volumes, restarting, updating (automatically, too), environment variables. |
| [**The web UI**](web-ui.md) | Every page, sign-in with Microsoft Entra ID, roles you can change, security, branding. |
| [**Configuration**](configuration.md) | The settings files, secrets, scheduling defaults, the room map, and extra bookings that aren't in 25Live. |

### Connecting your systems

| Page | What's in it |
|---|---|
| [**25Live setup**](25live.md) | The service account, the instance, and the `state` parameter. |
| [**BAS setup, by vendor**](bas-setup.md) | Automated Logic WebCTRL, Schneider EcoStruxure, Tridium Niagara, and any other BTL-listed controller. |
| [**Networking**](networking.md) | BACnet/IP addresses, BBMDs and foreign-device registration, and running in a datacenter or the cloud. |

### Operating it

| Page | What's in it |
|---|---|
| [**Reports and alerts**](reports-and-alerts.md) | The run report, email, Slack/Teams webhooks, and the dead-man's switch. |
| [**Safety rails**](safety.md) | Why a run refuses to stand the campus down, and when to `--force`. |
| [**The command line**](command-line.md) | Every command and exit code; running without Docker from Task Scheduler or cron; the desktop editor. |
| [**Upgrading**](upgrading.md) | What each release asks of you. |

### Elsewhere

- [CHANGELOG.md](../CHANGELOG.md) — every release's changes
- [CONTRIBUTING.md](../CONTRIBUTING.md) — development setup, tests, project layout, releasing
- [SECURITY.md](../SECURITY.md) — reporting a vulnerability
- The example settings files — `config.example.yaml`, `defaults.example.yaml`
  and `space_mapping.example.yaml` — document every setting they take.
