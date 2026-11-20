# Voice Clinic - local POC implementation and startup guide

This is a fictional GP receptionist demonstration: browser voice/text input, Azure
Speech, model routing, administrative SOP retrieval, governed tools, MySQL booking
transactions, Redis call state, and a live split-screen dashboard.

**Demonstration repository. Do not use real patient data.** The `.env` file and
other local secrets are ignored by Git. Installing packages/container images
downloads third-party software; running the configured AI providers sends synthetic
conversation/audio to those providers. This is not an offline AI system.

The original design is in [the implementation specification](./voice_agent_copilot_implementation_spec.md).

## 1. What you need

### Recommended: run all five services in Docker

- Docker Desktop running **Linux containers**:
  - Windows: use WSL2.
  - Intel Mac with macOS Sequoia (15.x): use **Docker Desktop for Mac with Intel
    chip**, not the Apple silicon installer. See the Mac walkthrough in section 3.
- Docker Compose v2 (`docker compose version`).
- A current Edge or Chrome browser; microphone permission for voice mode.
- Internet access for container/dependency downloads, the initial embedding model
  download, and your chosen cloud providers.
- Allow roughly 6-8 GB of Docker memory for MySQL, Next.js builds, PyTorch and
  speech/VAD. Actual consumption varies.
- Local ports 3000, 3001, 8000, 3306 and 6379 available.

You do **not** need local Python or Node.js when using the all-Docker path.

**Intel Mac compatibility:** run the complete stack in Docker. The Python
orchestrator requires `torch>=2.5`, whereas official native macOS x86_64 PyTorch
builds stopped after the 2.2 series. Therefore, do not use the Windows `uv sync`
instructions directly on an Intel Mac. Linux x86_64 containers avoid that wheel
restriction and run on CPU; neither CUDA nor Apple's MPS is required.
Setting `RAG_MODE=off` or disabling VAD does not remove the installation-time
PyTorch requirement.

### Alternative: run the applications on Windows

In addition to Docker for MySQL/Redis (or your own local instances):

- Node.js 22 LTS, including npm.
- Python 3.12 and `uv`.
- Supported Windows x64 for the Azure Speech Python SDK.

No application dependency installation was completed as part of implementation.
At your request, further installation was stopped. Python 3.12.14 had already
been installed before that instruction; the stopped initial npm command did not
produce a dependency lockfile. The commands below are for **you** to run.

## 2. Create ONE root environment file

On Windows, open PowerShell:

```powershell
Set-Location 'C:\Users\v-adholiya\Documents\VSCode-Workspace-Backup\Power App Demo\explorations\VoiceAgentPOC'
Copy-Item .env.example .env
```

On macOS, use the Terminal walkthrough in section 3 to create the same root
`.env` file. The cloud credentials and local environment values below are
identical on both operating systems.

Edit `.env` locally. Do not share it or put keys in any `NEXT_PUBLIC_*` variable.
The Python app, clinic process, Next.js config and Compose all
read this root file. No copies inside the application directories are needed.

### Required local values

| Variable | Set it to |
| --- | --- |
| `MYSQL_ROOT_PASSWORD` | Your own non-empty local MySQL root password |
| `MYSQL_PASSWORD` | A different non-empty password for `clinic_user` |
| `CLINIC_API_TOKEN` | A long random token, used only between the orchestrator and clinic |
| `MYSQL_DATABASE`, `MYSQL_USER` | Keep `gp_clinic` and `clinic_user` unless deliberately changing them |
| `ALLOWED_ORIGIN` | Keep `http://localhost:3000`; open the browser at exactly this origin |

Example random token generation using existing .NET APIs in PowerShell:

```powershell
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
[Convert]::ToBase64String($bytes)
$rng.Dispose()
```

Copy the output into `CLINIC_API_TOKEN`. Generate independent values for passwords.
On macOS, use `openssl rand -base64 32` instead; no .NET installation is needed.

### Cloud API keys and Azure details

| Capability | Variables | Where you obtain them |
| --- | --- | --- |
| Voice input and output | `AZURE_SPEECH_KEY`, `AZURE_SPEECH_REGION` | In your own Azure subscription, create/use a **Speech** resource. Azure Portal > resource > Keys and Endpoint. Use one key and the resource's actual region, e.g. `uksouth`. |
| Main conversational model | `GITHUB_TOKEN`, `GITHUB_MODELS_BASE_URL`, `PRIMARY_REASONING_MODEL` | GitHub Models' inference API was retired in July 2026; this provider is not currently available. |
| Optional OpenAI-compatible model | `GROQ_API_KEY`, `GROQ_BASE_URL`, `FAST_ROUTER_MODEL` | A Groq key with an active tool-capable model, or a Gemini API key with `https://generativelanguage.googleapis.com/v1beta/openai/` and an available Gemini model (for example `gemini-3.5-flash-lite`). Google AI Pro benefits do not pay for external API usage; check API billing separately. |
| Optional Azure SOP index | `AZURE_SEARCH_ENDPOINT`, `AZURE_SEARCH_API_KEY`, `AZURE_SEARCH_INDEX_NAME` | Your Azure AI Search service endpoint, an admin key for initial ingestion, and a dedicated index name. Use a query key at runtime after ingesting. Not needed for default local RAG. |

**For a complete voice demo:** supply a Speech key/region and at least one model
provider key. Supplying both model providers enables tiered routing. With just
one configured, that provider handles all turns; there is no silent retry against
another provider on errors.

No Azure subscription ID, tenant ID, resource group, managed identity, Azure
OpenAI deployment, Foundry project, or Azure CLI sign-in is required by this code.
You need your subscription only to obtain the Speech resource and, optionally,
Search. Nothing here creates those resources for you.

Free SKUs/model quotas may be available but are **not guaranteed**. Check your
subscription/provider pricing, regional availability, limits and data policies.
The implementation deliberately does not promise a zero-cost service.

### Other settings

- `AZURE_SPEECH_LOCALE=en-GB` and `AZURE_TTS_VOICE=en-GB-SoniaNeural` are defaults.
- `VAD_ENABLED=true` enables local Silero speech-onset detection. Partial STT also
  triggers interruption. Use headphones to avoid the synthetic voice causing
  echo/barge-in.
- `RAG_MODE=local` uses local dense embeddings + BM25 + reciprocal rank fusion.
  First use downloads `BAAI/bge-small-en-v1.5` from its public model repository.
  Do not place real clinical material into the SOP folder.
- `RAG_MODE=off` explicitly disables retrieval for initial wiring checks. It is
  not an automatic fallback when RAG fails.
- `NEXT_PUBLIC_*` values are non-secret browser URLs, baked in at dashboard build
  time. Changing them requires rebuilding/restarting the dashboard.

## 3. Start everything - recommended Docker route

From the POC root, after saving `.env`. The `docker compose` commands in this
guide work in both PowerShell and macOS Terminal; only the Windows path,
environment-file and `Invoke-RestMethod` commands are OS-specific.
For a complete Mac-specific sequence, use the walkthrough below.

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail 80 clinic orchestrator dashboard
```

The first build installs dependencies **inside containers**; expect several
minutes and substantial downloads. MySQL initialization seeds synthetic data.
The orchestrator does not require cloud credentials just to start, but free-form
conversation and voice require the keys described above.

Check readiness:

```powershell
Invoke-RestMethod http://localhost:3001/health
Invoke-RestMethod http://localhost:8000/health
Invoke-RestMethod http://localhost:3001/bookings/available
```

The orchestrator health response should show:

- `status: ok`, `redis: ok`, `clinic: ok`
- `model_configured: true` for conversation
- `speech_configured: true` for voice

These configuration flags check that values are present, not that the provider
has accepted the credentials. An actual turn verifies credentials and quota.

Open **http://localhost:3000** (not `127.0.0.1`, because the origin is checked).
Start with **Text**, then try **Microphone + Azure Speech** and grant permission.
Allow an initial model download/warm-up on the first conversational turn.

Stop without deleting the database:

```powershell
docker compose down
```

#### Stop and restart later without rebuilding

For regular stop/start, run these from the project folder. Docker keeps the
downloaded images and dependencies, and `docker compose down` keeps the MySQL
data volume. Do not add `-v` unless you intend to delete that database volume.

macOS Terminal:

```sh
cd '/Users/apple/Desktop/Work artiffacts/VoiceAgentPOC'
docker compose down
docker compose up -d --no-build
docker compose ps
curl --fail --silent --show-error http://localhost:8000/health
open http://localhost:3000
```

On Windows PowerShell, first change to the folder where you saved the project,
then use the same `docker compose down`, `docker compose up -d --no-build`,
`docker compose ps`, and health-check commands. Open
`http://localhost:3000` in your browser.

`--no-build` reuses the images already on this computer; it does not install
dependencies again. Docker Desktop must be running. The health response should
include `"status":"ok"`. To start the already-built stack without stopping it,
just run `docker compose up -d --no-build`.

To apply changed `.env` values:

```powershell
docker compose up --build -d --force-recreate
```

This does not reset existing MySQL credentials. MySQL image initialization uses
the password variables only when its data volume is first created.

### Intel Mac + macOS Sequoia: complete Terminal walkthrough

#### A. Prepare the Mac

1. Copy the POC **source folder** locally to the Mac, including `.env.example`,
   `.dockerignore` and the three application folders. Keep it out of cloud-synced
   folders. Do not copy Windows `node_modules`, `.venv`, `.next` or `dist` folders;
   container builds restore their own Linux dependencies. Create credentials
   locally rather than transferring a populated `.env`.
2. Download [Docker Desktop for Mac with Intel chip](https://docs.docker.com/desktop/setup/install/mac-install/),
   install it in Applications, launch it and complete its initial setup. Wait for
   the Docker engine to be running. Check Docker's licensing requirements for
   your organisation.
3. In Docker Desktop's resource settings, allow approximately 6-8 GB memory for
   this stack if your Mac has sufficient RAM; a 16 GB host is preferable.
   The initial PyTorch/Next.js builds also need several GB of free disk space.
   No WSL2, Rosetta, GPU setup, Homebrew, host Python or host Node.js is needed
   for this route.
4. Use a current Chrome or Edge browser for the initial voice acceptance check.
   Safari-specific audio behaviour has not been validated.

Open **Terminal** (the default zsh shell) and check:

```sh
sw_vers -productVersion
uname -m
docker compose version
docker info --format '{{.OSType}}/{{.Architecture}}'
```

Expect macOS `15.x`, host `x86_64`, and a running Linux/x86_64 Docker engine
(Docker may label the architecture `amd64`). The Intel Docker installer and
default architecture selection are sufficient; do not force `linux/arm64`.
Use a Docker Desktop release whose published macOS requirements include Sequoia;
Docker's supported macOS window changes over time.

#### B. Create and populate the root environment file

The following assumes you copied the folder to `~/Documents/VoiceAgentPOC`.
Replace that path with your actual local location; quote paths containing spaces.

```sh
cd "$HOME/Documents/VoiceAgentPOC"
if [ ! -f .env ]; then cp .env.example .env; fi
openssl rand -base64 32
nano .env
```

Use the generated value for `CLINIC_API_TOKEN`; generate separate local database
passwords. Fill in the Speech and model credentials from section 2. Save in nano
with Ctrl+O, Enter, then Ctrl+X. Keep the filename exactly `.env`, not `.env.txt`.
Do not `source .env`; Docker Compose reads it directly.

Keep `ALLOWED_ORIGIN=http://localhost:3000` and the default browser URLs.
Compose automatically changes the internal Redis and clinic addresses to Docker
service names, so no Mac-specific Azure endpoints or application configuration
changes are required.

#### C. Build, start and check readiness

From that same folder:

```sh
docker compose config --quiet
docker compose up --build -d
docker compose ps
docker compose logs --tail 80 clinic orchestrator dashboard
```

These are installation/build commands for **you** to run. The first build
downloads dependencies; nothing needs installing into macOS Python. If a service
is still starting, wait and check its logs before the following readiness calls:

```sh
curl --fail --silent --show-error http://localhost:3001/health
curl --fail --silent --show-error http://localhost:8000/health
curl --fail --silent --show-error http://localhost:3001/bookings/available
```

The health response expectations listed earlier in this section apply unchanged.
After the containers start, this import check verifies the installed Linux
runtime and the two key native dependencies without calling a cloud API:

```sh
docker compose exec orchestrator python -c "import platform, torch; import azure.cognitiveservices.speech; from importlib.metadata import version; print(platform.system(), platform.machine()); print('PyTorch', torch.__version__); print('Speech SDK', version('azure-cognitiveservices-speech'))"
```

Expect `Linux x86_64` and installed dependency versions. This is a Mac-side check
to perform after building, not a claim that those imports were run on a Mac
during implementation.

#### D. Browser and microphone permissions

```sh
open http://localhost:3000
```

Start a **Text** session first. Then end it, select **Microphone + Azure Speech**,
start a new call and allow the browser's microphone prompt.

On Sequoia, also check **System Settings > Privacy & Security > Microphone** and
enable the browser if necessary; quit/reopen the browser after changing access.
Check the site's microphone permission and the selected input device. Use
headphones and keep the tab active while testing.

The browser captures the Mac microphone and sends PCM over WebSocket. Docker
does not need microphone/device passthrough, host audio drivers or macOS Speech
SDK installation. Always open `http://localhost:3000`, not `127.0.0.1` or a LAN IP.
Follow all booking, prescription, emergency and interruption checks in section 5.

#### E. Stop or apply configuration changes

```sh
# Stop, retaining the synthetic database:
docker compose down

# After editing .env, rebuild/recreate:
docker compose up --build -d --force-recreate
```

The optional Search ingestion, Docker logs and named-volume reset commands later
in this guide also run unchanged in macOS Terminal, even where their code fence
is labelled PowerShell. Do not use `npm.cmd`, `Invoke-RestMethod` or Windows drive
paths on the Mac. Native Python installation on Intel macOS is **not** a
supported alternative for the current dependency manifest; do not downgrade
PyTorch or install unofficial wheels merely to bypass the resolver.

## 4. Alternative: Windows application processes

Run only the dependencies in Docker:

```powershell
docker compose up -d mysql redis
```

Keep the host addresses from `.env.example`: `MYSQL_HOST=localhost`,
`REDIS_URL=redis://localhost:6379/0`, and
`NESTJS_BACKEND_URL=http://localhost:3001`.

### Terminal 1 - clinic API

```powershell
Set-Location 'C:\Users\v-adholiya\Documents\VSCode-Workspace-Backup\Power App Demo\explorations\VoiceAgentPOC\clinic-backend-nestjs'
npm.cmd install --no-audit --no-fund
if ($LASTEXITCODE -eq 0) { npm.cmd run build }
if ($LASTEXITCODE -eq 0) { npm.cmd start }
```

### Terminal 2 - orchestrator

```powershell
Set-Location 'C:\Users\v-adholiya\Documents\VSCode-Workspace-Backup\Power App Demo\explorations\VoiceAgentPOC\ai-orchestrator'
uv sync --python 3.12
if ($LASTEXITCODE -eq 0) { uv run --no-sync uvicorn app.main:app --host 127.0.0.1 --port 8000 }
```

`uv sync` restores Python packages into this application's `.venv`. It may
download Python 3.12 if not installed. Use the 3.12 runtime already installed on
this machine if it is still available.

### Terminal 3 - dashboard

```powershell
Set-Location 'C:\Users\v-adholiya\Documents\VSCode-Workspace-Backup\Power App Demo\explorations\VoiceAgentPOC\clinic-dashboard-nextjs'
$env:NEXT_TELEMETRY_DISABLED = '1'
npm.cmd install --no-audit --no-fund
if ($LASTEXITCODE -eq 0) { npm.cmd run dev }
```

`npm.cmd` avoids the `npm.ps1` execution-policy issue on this machine. No global
execution-policy change is necessary. Use Ctrl+C to stop each process.

To validate production builds after installing dependencies:

```powershell
# In clinic-backend-nestjs:
npm.cmd run build

# In clinic-dashboard-nextjs:
npm.cmd run typecheck
npm.cmd run build
```

No CI or automated test framework was added, in accordance with the exploration
instructions. For reproducibility, retain the local lockfiles produced by your
first successful installs; do not upload them or any other POC files.

## 5. Manual acceptance walkthrough

Use only the seeded fictional identity:

- Full name: **Alex Demo**
- Date of birth: **1990-01-15**
- Existing medication: **Metformin**

### Booking

1. Start a text session.
2. Ask: "What appointments are available?"
3. Choose one of the two slots offered and give the synthetic name and DOB.
4. The server repeats the details. No database booking should exist yet.
5. Say exactly "yes" within 30 seconds of the slot hold being created.
6. Only after MySQL commits should the confirmation and dashboard booking appear.
7. In another session, attempt the same slot. It must not be double-booked.

Slots are seeded one and two days after **first database initialization**, not a
fixed calendar date. Displayed clinic appointment times use Europe/London;
audit timestamps use UTC. If your POC sits unused for several days, the initial
slots will expire. Add new synthetic future slots through your local MySQL client,
or deliberately reset just this POC's database using the instructions below.

### Confirmation / cancellation / expiry

- Say "no" before the first submission attempt: nothing is submitted.
- Let the 30-second hold expire, then say "yes": it must ask you to check
  availability again, not claim success.
- Interrupt before the voice read-back finishes: "yes" must not immediately
  commit. The server repeats the confirmation.
- A timeout while saving is an **unknown outcome**, not a successful booking.
  Inspect the bookings dashboard. Retrying "yes" in the same pending call uses
  the same idempotency key. Do not assume a disconnected request was cancelled.

### Prescriptions

- Ask for prescription status using the synthetic identity; the seeded record
  is `READY`.
- Request a repeat of Metformin, provide the identity, and confirm the repeated
  details. The result is `REQUESTED`, **not approval to dispense**.
- An unknown patient or a new medication must fail explicitly.

### Emergency demonstration

- In a fresh session, type "I have chest pain".
- The deterministic emergency script should run without a model request.
- An emergency alert should appear and routine transactions remain disabled
  for that call, even on subsequent turns.
- End the call to begin a separate non-emergency demonstration.

The detector is intentionally conservative and can false-trigger on negated or
historical symptoms. It cannot recognise every emergency and is **not** a
clinical triage system or evidence of DCB0129 compliance.

### Voice and interruption

- Use headphones, start voice mode and speak naturally.
- Verify partial/final recognition and spoken audio.
- Speak while the receptionist is responding, or use Interrupt.
- Buffered browser audio should stop; a new final transcript starts the next
  turn. A transaction already being saved after confirmation is not cancelled.
- The first-audio badge reports server time from final STT text to first
  synthesized PCM. It excludes endpointing, network and browser playback delay.
  `<800 ms` is a performance target, not a verified service guarantee.

## 6. Optional Azure AI Search

Default local RAG needs **no Azure Search**.

Only if you want to use your own Search resource:

1. Set `AZURE_SEARCH_ENDPOINT`, the **admin** key and a dedicated
   `AZURE_SEARCH_INDEX_NAME` in the root `.env`.
2. Do not point this command at a shared/production index. It creates or updates
   that index's schema and uploads the local synthetic SOP chunks.
3. With Docker:

   ```powershell
   docker compose up -d --force-recreate orchestrator
   docker compose exec orchestrator python -m app.rag.ingest
   ```

   Or from the Windows orchestrator directory after `uv sync`:

   ```powershell
   uv run --no-sync python -m app.rag.ingest
   ```

4. Replace the admin key with a **query** key and set `RAG_MODE=azure`.
5. Recreate/restart the orchestrator.

Dense embeddings are still computed locally; Azure runs separate vector and
BM25 queries, combined by the application's pure-Python RRF. Keep the embedding
model consistent between ingestion and querying. Reingest if it changes. Removed
SOP files are not automatically deleted from an existing Azure index; use a new
dedicated index for a clean corpus.

## 7. Local interfaces and data flow

| Service | Interface |
| --- | --- |
| Dashboard | `http://localhost:3000` |
| Clinic API | `http://localhost:3001/health`, `/bookings/available`, `/bookings`, `/events` |
| Private mutations | `POST /bookings/confirm`, `/prescriptions/request`, `/prescriptions/status`, `/audit`; require `x-clinic-token` |
| Orchestrator | `http://localhost:8000/health`, `/docs` |
| Session | `POST /api/sessions` from the configured dashboard origin |
| Voice/text | `/ws/voice?call_id=...&token=...&voice=true/false` |
| Call telemetry | `/api/telemetry?call_id=...&token=...` (SSE) |

The browser supplies binary **16 kHz mono PCM16** frames from an AudioWorklet,
not WebM/Opus. Output audio is the same raw PCM format. JSON events carry
transcripts, interruptions, turn boundaries, safety alerts, errors and metrics.
Each call has an unguessable token, a single-active-connection lease and a
one-hour sliding Redis lifetime. Telemetry is scoped by call credentials.
Disconnects release uncommitted holds. MySQL remains the authoritative booking
state; Redis holds are advisory, not the concurrency guarantee.

Tool input schemas, allowlist checks and audit calls are shared by the embedded
agent tool loop and an optional **local stdio FastMCP adapter**:

```powershell
# From ai-orchestrator, after installing dependencies:
uv run --no-sync python -m app.mcp_tools.server
```

It is not registered in your VS Code MCP configuration automatically. The adapter
requires an existing call ID/token and a disconnected browser session. Mutation
tools only prepare pending requests; the adapter cannot bypass the browser
read-back and next-turn confirmation. Do not connect it to an external service.

## 8. Troubleshooting

| Symptom | Action |
| --- | --- |
| `docker` not found | Install/start Docker Desktop for your OS/architecture and open a new Terminal if its CLI path changed. On Intel Mac, use the Intel installer and the section 3 walkthrough. Docker was not available on the validation machine. |
| Intel Mac `uv sync` cannot resolve `torch>=2.5` | Native macOS x86_64 wheels are unavailable for this requirement. Use the all-Docker route; disabling RAG/VAD does not change dependency resolution. |
| Mac build says `exec format error` or wrong architecture | Confirm `uname -m` is `x86_64` and Docker's engine is Linux/x86_64. Remove any unintended ARM platform override before rebuilding; no ARM emulation is required on this Intel Mac. |
| Docker bind-mount access denied on Mac | Keep the POC in a local user folder; allow Docker Desktop access to that folder through its file-sharing settings/macOS prompts. Do not solve it with broad filesystem permission changes. |
| Mac microphone is silent/denied | Allow Chrome/Edge in Sequoia's Privacy & Security > Microphone and in the site's settings; verify the input device and reopen the browser. No Docker microphone mount is needed. |
| MySQL unhealthy after password edits | The existing volume retains original credentials. Restore the original values or explicitly reset only this POC's volume. |
| Browser CORS/origin error | Use exactly `http://localhost:3000`; match `ALLOWED_ORIGIN` if deliberately changing the URL. |
| `401`, `403` or model not found | Check the relevant key, GitHub Models permission/model ID, region, provider access and quota. Restart after edits. |
| Model `429` | Provider rate limit; wait or adjust your provider/account. No silent fallback occurs. |
| First turn slow | Embedding model download/initialization and cloud cold starts. Subsequent turns should be faster. |
| Speech denied/no audio | Check Speech key/region, microphone permission, browser autoplay permission and device. Start text mode to isolate model/API wiring. |
| Azure Speech shared library error | Use the supplied Linux image (also on Intel Mac) or a supported Windows x64 SDK environment. |
| Embedding download blocked | Allow the public model download, pre-cache it locally, or explicitly choose `RAG_MODE=off` while validating other wiring. |
| New `.env` has no effect | Recreate Docker services or restart Windows processes; rebuild dashboard for public URL changes. |
| Agent says no slots while the dashboard lists them | Ensure the latest clinic and orchestrator code is running. Availability lookup treats “any doctor” as no filter, matches common `Dr`/`Doctor` name variants, and avoids applying a past year inferred by the model. The spoken response is based on the returned slots. |
| No available slots in both the agent and dashboard | Original seeded dates may have passed or slots may be booked. Add future synthetic slots or reset this POC only. |
| SSE reconnecting | Verify API services and logs. Booking tables also poll every 15 seconds. Call transcript remains on WebSocket. |

Useful Docker logs:

```powershell
docker compose logs --tail 100 orchestrator
docker compose logs --tail 100 clinic
docker compose logs --tail 100 dashboard
```

Avoid sharing logs containing keys or patient-like information. Do not publish
the output of `docker compose config` without `--quiet`: resolved configuration
can contain credentials.

### Deliberately reset this POC's synthetic database

**Destructive: deletes only the synthetic database stored in this POC's named
volume. Do not run if you need its bookings/audit history.**

```powershell
docker compose down
docker volume inspect voice-agent-poc_mysql-data
# Only after confirming this is the intended POC volume:
docker volume rm voice-agent-poc_mysql-data
docker compose up -d
```

Do not use broad prune commands or delete other projects' volumes.

## 9. Implementation notes and validation boundary

### Intel Mac compatibility sanity review

- All four upstream image tags used by Compose/Dockerfiles were checked against
  Docker Hub's registry metadata: `python:3.12-slim-bookworm`,
  `node:22-bookworm-slim`, `mysql:8.4` and `redis:7-alpine` publish `linux/amd64`.
- PyPI metadata confirms Linux x86_64 wheels usable with Python 3.12 for
  PyTorch 2.5.1 and Azure Speech SDK 1.40.0, within the declared dependency ranges.
  This confirms representative wheel availability, not a full dependency solve.
- The application Dockerfiles and npm scripts do not invoke Windows-only
  commands. Environment paths are resolved relative to source/config files,
  Compose uses relative mounts, and the browser uses standard Web Audio APIs.
  Internal Python import casing was checked for Linux filesystems. All seven
  Mac shell snippets passed syntax-only parsing with an existing Bash; none of
  their installation/startup commands were executed. The browser PCM worklet
  again produced exactly 16,000 output samples per second from 16/44.1/48 kHz
  synthetic input in Node's VM.
- No application source change or dependency downgrade was required for the
  Intel Mac **Docker route**. Native Intel macOS Python is explicitly excluded
  because of the current PyTorch requirement.
- This was a source/metadata sanity review from Windows, **not an actual macOS
  execution test**. No dependencies, container images or model weights were
  installed/downloaded for this review. Full builds, container startup, cloud
  credentials, Mac microphone/playback, latency and end-to-end acceptance still
  require the Mac-side checks above.

Official platform references:
[Docker Desktop Mac prerequisites](https://docs.docker.com/desktop/setup/install/mac-install/)
and [PyTorch's macOS x86_64 binary deprecation announcement](https://dev-discuss.pytorch.org/t/pytorch-macos-x86-builds-deprecation-starting-january-2024/1690).

### General implementation notes

- Next.js 16/React 19 are used instead of the older Next.js 14 line in the sketch.
  The application remains a Next.js App Router/Tailwind/TanStack Query UI.
- The current GitHub Models endpoint and provider-qualified default model ID are
  configurable rather than relying on the older endpoint in the sketch.
- Safety and confirmation are server-side state transitions, not prompt-only
  claims. Booking and prescription retries have idempotency keys.
- Slot locking uses MySQL `FOR UPDATE NOWAIT`; booking and its audit commit in
  one transaction. Redis holds use owner-checked release.
- Speech synthesis streams raw audio chunks rather than waiting for a full
  sentence's audio before transmission.
- No packages are silently installed by an application at runtime. The embedding
  model's first-use public download is the exception, documented above.
- Python/TypeScript syntax, JSON/TOML and Compose YAML structure were checked
  locally using existing tools. Dependency-free checks covered safety examples,
  sentence chunking, RRF, and PCM conversion (exactly 16,000 output samples for a
  second of 16/44.1/48 kHz input). The real confirmation methods were exercised
  with fake IO for announcement gating, expiry, ownership-safe release, emergency
  blocking, surfaced timeouts and stable-key retries after hold expiry.
  Full TypeScript builds, dependency restoration, MySQL/Redis integration,
  Docker images, cloud API access, real microphone behavior and latency must be
  verified after **you** install dependencies and provide credentials.

### Not production-ready

No clinical certification, diagnosis, real prescribing, real patient verification,
telephony/PSTN, appointment cancellation, authentication/RBAC for human staff,
TLS, encryption-at-rest controls, durable telemetry delivery, rate limiting,
HA, migrations, monitoring, clinical evaluation or regulatory approval is provided.
Public read endpoints intentionally expose **only synthetic non-identity booking
information**. Local origin checks and call tokens are not production identity
security. Never expose these ports to a LAN or the internet.

Before any real deployment, perform professional clinical safety, privacy,
security, accessibility and operational review; replace all synthetic policies,
credentials and data handling with approved implementations. This POC does not
establish NHS or DCB0129 compliance.