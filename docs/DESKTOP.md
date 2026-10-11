# The OpenBerry desktop app

The desktop app is OpenBerry packaged as a normal program. You download it, double-click it, and the
dashboard opens in its own window. You don't need a terminal or Python, and nothing is installed
system-wide. Your data stays on your computer.

- [Download](#download)
- Install it on [Windows](#windows), a [Mac](#mac) or [Linux](#linux)
- [Connect Claude Desktop](#connect-claude-desktop)
- [Your data and settings](#your-data-and-settings), [backups](#back-up-your-data) and [restoring one](#restore-a-backup)
- [Updating](#updating) ([from 0.1.x](#upgrading-from-01x)), [closing the app](#closing-the-app) and
  [uninstalling it](#uninstall)
- [If something goes wrong](#if-something-goes-wrong)
- For developers: [building it yourself](#building-it-yourself) and [releasing a new version](#releasing-a-new-version)

## Download

Open the [Releases page](https://github.com/connexionlimodubai-pixel/gj/releases), click the newest
release, and under **Assets** download the file for your computer:

| Your computer | The file to download |
|---|---|
| Windows 10 or 11 | `OpenBerry-windows-x64.zip` |
| Mac with Apple silicon (M1 or newer), macOS 11 or later | `OpenBerry-macos-arm64.zip` |
| Linux, 64-bit | `OpenBerry-linux-x64.tar.gz` |

The file names are the same for every version (version 0.1.0 used names like `OpenBerry-0.1.0-windows-x64.zip`); the release title says which version you have.
`SHA256SUMS` in the same list holds each file's checksum. To check a download on Windows, run
`certutil -hashfile OpenBerry-windows-x64.zip SHA256` in a terminal and compare; on a Mac or Linux, `shasum -a 256 <file>`.

There is no download for Macs with an Intel processor yet. To check which kind you have, open the
Apple menu and click **About This Mac**: "Chip: Apple M…" means Apple silicon.

**No release yet?** Every change to the app is also built automatically, and those builds are kept for 14 days.
1. Sign in to GitHub.
2. Open the repository's **Actions** tab and click **Desktop app**.
3. Click the newest run with a green tick.
4. Scroll down to **Artifacts** and download the one for your computer.

GitHub wraps each of these downloads in an extra zip file. Unzip it first to get the file in the table above.

## Windows

1. **Unzip it.** Right-click the downloaded `.zip` file, choose **Extract All…**, and pick a folder
   where the app can stay. We suggest `%LOCALAPPDATA%\Programs` (type it in the box, Windows turns it into
   `C:\Users\<you>\AppData\Local\Programs`). Avoid **Documents** and **Desktop**: OneDrive often syncs
   them, and moves or locks the app's files. Don't run the app from inside the zip file.
2. **Open it.** Open the new `OpenBerry` folder and double-click **OpenBerry** (`OpenBerry.exe`).
3. **The first time**, Windows may show a blue box saying *"Windows protected your PC"*. Click
   **More info**, then **Run anyway**. Windows shows this box because the app isn't code-signed: a signing
   certificate costs money, and OpenBerry is free. The app is built by GitHub from the public source code.
4. OpenBerry opens in its own window.

Keep the files in the `OpenBerry` folder together. `openberry-cli.exe` and the `_internal` folder are part
of the app. To open the app more easily, right-click `OpenBerry.exe` and choose **Send to → Desktop
(create shortcut)**, or pin it to the taskbar once it is open.

The window uses Microsoft Edge WebView2, which Windows 10 and 11 already include. If it is missing
(this is rare, mostly on old Windows 10 PCs), OpenBerry opens in your web browser instead. Install the free
[WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) from Microsoft to get the window.

## Mac

1. **Unzip it.** Double-click the downloaded `.zip` file. Safari often unzips it for you.
2. **Move it to Applications.** Drag **OpenBerry** into your **Applications** folder. Do this before you
   connect Claude, because Claude remembers where the app is.
3. **The first time**, macOS blocks the app because it isn't notarised by Apple. Notarisation needs a paid Apple
   developer account. To open it anyway:
   - **macOS 15 (Sequoia) or later:**
     1. Double-click OpenBerry. When macOS says it can't open it, click **Done**.
     2. Open **System Settings → Privacy & Security** and scroll down.
     3. Next to *"OpenBerry" was blocked*, click **Open Anyway**.
     4. In the box that appears, click **Open Anyway** again, and enter your password if macOS asks for it.
   - **macOS 14 (Sonoma) or earlier:** in Applications, right-click (or Control-click) OpenBerry, choose
     **Open**, then click **Open** again.

   After the first time, OpenBerry opens normally. macOS shows this warning because the app isn't notarised,
   not because something is wrong with it: the app is built by GitHub from the public source code.

## Linux

1. Extract it: right-click the `.tar.gz` file and choose **Extract Here**, or run
   `tar -xzf OpenBerry-linux-x64.tar.gz`.
2. Run `./OpenBerry/OpenBerry` from a terminal. The dashboard opens in your web browser. The Linux app has
   no window of its own: that would need system libraries (GTK or Qt) that differ between Linux distributions.
3. To stop OpenBerry, press **Ctrl+C** in that terminal. If you started it by double-clicking, there is no
   Quit button: run `pkill -f OpenBerry/OpenBerry`, or end it in your system monitor.

## Using the app

- The first time, choose **Register your company** and fill in the form, or [connect Claude](#connect-claude-desktop)
  and ask it to interview you and register your company.
- The dashboard runs on your computer at `http://127.0.0.1:8000`, or at the next free number up to 8099
  if 8000 is taken. Only your own computer can open it.
- If you open the app while it is already running, it doesn't start a second copy. On a Mac it brings
  OpenBerry to the front; on Windows it opens another window onto the same dashboard.

## Connect Claude Desktop

Claude does the research and writing. It uses OpenBerry's tools through the program `openberry-cli`, which
comes with the app.

1. In OpenBerry, click **Connect Claude** in the menu.
2. In the **Claude Desktop** box, click **Copy**. The copied settings already point at the app on
   your computer.
3. In Claude Desktop, open **Settings → Developer → Edit Config**. This opens a file called
   `claude_desktop_config.json`.
   - If the file is empty or only contains `{}`, replace everything in it with what you copied.
   - If it already has an `"mcpServers"` section, add the `"openberry": { … }` part inside that section,
     with a comma between it and the entry before it.
4. Save the file, quit Claude Desktop completely, then open it again. On Windows, right-click Claude's
   icon next to the clock and choose **Quit**. On a Mac, use **Claude → Quit Claude**.
5. Ask Claude: *"Use openberry: list my companies."*

To let Claude send approved LinkedIn messages from your own Chrome (optional, off by default, at your own
risk), see [AI agent sending](AI_AGENT_SENDING.md).

The settings look like this. The paths are examples, and your own are in the **Connect Claude** page:

```json
{
  "mcpServers": {
    "openberry": {
      "command": "/Applications/OpenBerry.app/Contents/MacOS/openberry-cli",
      "args": ["mcp"],
      "env": { "OPENBERRY_DB": "/Users/you/.openberry/openberry.db" }
    }
  }
}
```

On Windows the command is like `C:\\Users\\you\\AppData\\Local\\Programs\\OpenBerry\\openberry-cli.exe`: in this file every
`\` is written twice. The copied settings already do that for you.

- **When OpenBerry is closed,** Claude can still use its tools: Claude starts `openberry-cli` in the
  background, and it uses the same data. The links in Claude's answers only open while the app is open.
- **If you move the app** to another folder, open **Connect Claude** again and copy the new settings
  into Claude Desktop.
- **A yellow warning on the Connect Claude page** means OpenBerry is running from a temporary place: on a Mac,
  straight from the Downloads folder instead of Applications; on Windows, from inside the zip file. Claude
  wouldn't find it there later. Do what the warning says, then copy the settings.

## Your data and settings

Everything OpenBerry stores is in a folder named `.openberry` in your home folder:

| Computer | Folder |
|---|---|
| Windows | `C:\Users\<you>\.openberry` |
| Mac | `/Users/<you>/.openberry`. The folder is hidden: in Finder, press **Cmd+Shift+G** and type `~/.openberry` |
| Linux | `~/.openberry` |

Inside the folder:

- **`openberry.db`** holds all your companies, leads, signals, messages and email opt-outs. While OpenBerry or
  Claude uses it, the newest changes are in `openberry.db-wal` next to it. [Back it up](#back-up-your-data) from the
  app rather than copying it.
- **`backups/`** holds the copies OpenBerry saves by itself (see [Back up your data](#back-up-your-data)).
- **`.env`** holds your settings and keys, if you have any (see below), and your mailbox password.
- **`logs/desktop.log`** records what the app did. Look here, or send it along, when something goes wrong.
- **`webview/`** and **`desktop-secret-key`** let the window remember its session between runs, such as the company
  you last opened. The app has no login (unless you set `OPENBERRY_PASSWORD`).

**Settings.** OpenBerry works without any settings. For optional features, create a plain-text file called
`.env` in the `.openberry` folder with one `NAME=value` per line. For example:

```
OPENBERRY_CONTACT_EMAIL=you@example.com
GITHUB_TOKEN=ghp_your_token
```

Then close OpenBerry and open it again. The [README](../README.md#configuration) and
[`.env.example`](../.env.example) list every setting.

Some settings can be entered in the app: open **API keys** in the menu to add your Google Maps key. It is saved in the
same `.env` file, which only your user account can read, and you don't need to restart.

To let OpenBerry email the emails you approve from your own mailbox, open **Mailbox** in the menu, choose your
provider (Zoho Mail, Hostinger or Gmail), and type your address and password (or app password). The password is saved
in the same `.env` file and never shown again. Then turn on **Email sending** on a company's Outreach page. Emails go
out only while the app is open; close it and sending waits. Read [EMAIL_SENDING.md](EMAIL_SENDING.md) first: some
providers, Hostinger for example, forbid email to people who did not opt in.

## Back up your data

- **Download backup:** open **Your data** in the menu and click **Download backup**. You get one file,
  `openberry-backup-<date>.db`, with all your companies, leads, messages and email opt-outs, made safely while
  OpenBerry runs. Keep it somewhere else too: another drive, a USB stick or cloud storage. Your settings and
  passwords (`.env`) are not in it.
- **Automatic backups:** while it is open, OpenBerry saves a copy in `.openberry/backups` every day, and keeps 7 days
  and 4 weeks of them. It also saves one before an update changes your data, before you delete a company and before a
  restore. **Your data** lists them, and each one can be downloaded there.
- **From the command line:** `openberry-cli backup` saves a copy in the backups folder, and `openberry-cli backup
  D:\Backups` saves it in that folder. It works while OpenBerry is open.
- **By hand:** close OpenBerry **and** quit Claude Desktop (it keeps `openberry-cli` running), then copy the whole
  `.openberry` folder, with every file in it: `openberry.db`, `openberry.db-wal` and `openberry.db-shm` if they are
  there, and `.env`. Copying `openberry.db` alone while either is open can miss your latest changes.

## Restore a backup

Restoring replaces all your data with the backup's.

1. Close OpenBerry, and quit Claude Desktop (right-click Claude's icon next to the clock, **Quit**; on a Mac,
   **Claude → Quit Claude**).
2. Open a terminal (on Windows, **Command Prompt**) and run the app's `openberry-cli` with `restore` and the backup
   file. **Your data → Restore a backup** shows the exact line for your computer, for example:
   ```
   "C:\Users\you\AppData\Local\Programs\OpenBerry\openberry-cli.exe" restore "C:\Users\you\Downloads\openberry-backup-2026-10-11.db"
   ```
3. Open OpenBerry and Claude Desktop again.

OpenBerry checks the backup first, and refuses one that is damaged or was saved by a newer version. It saves your
current data in `backups/` before replacing it. Then it turns **email sending and AI agent sending off** for every
company: emails sent after the backup was made are not in it, so they would go out a second time. Check the
Outreach pages, then turn sending back on.

## Updating

1. **Back up:** in OpenBerry, open **Your data** and click **Download backup**. Version 0.1.x has no Your data page:
   after step 2, copy the whole `.openberry` folder instead ([by hand](#back-up-your-data)).
2. Close OpenBerry, and **quit Claude Desktop** (right-click Claude's icon next to the clock, **Quit**; on a Mac,
   **Claude → Quit Claude**). Claude Desktop keeps the old `openberry-cli` running in the background until you quit it.
3. Download the new version.
4. Replace the old app with it:
   - **Windows:** delete the old `OpenBerry` folder, then extract the new one in the same place.
   - **Mac:** drag the new OpenBerry into Applications and choose **Replace**.
   - **Linux:** extract the new version over the old one.
5. Open the new version. The first time, Windows or macOS asks you to confirm again, as when you installed it.
   If the new version stores data differently, it updates your data by itself, after saving a copy in
   `.openberry/backups` (named `openberry-pre-v…`).
6. Open Claude Desktop again.

Your data in `.openberry` stays as it is. If you put the new version in a different place, copy the
Claude Desktop settings again ([Connect Claude Desktop](#connect-claude-desktop)).

**There is no going back to an older version** once a newer one has updated your data. From 0.2.0 on, an older
version refuses to open it ("This data was saved by a newer OpenBerry"). Version 0.1.x doesn't check and can damage
it, which is why step 2 quits Claude Desktop: it may still run the old `openberry-cli`. To go back anyway, put the
backup from step 1 back first ([restore](#restore-a-backup), or copy the folder back by hand while everything is
closed).

Coming from 0.1.x? Read [Upgrading from 0.1.x](#upgrading-from-01x) first.

## Upgrading from 0.1.x

Version 0.2.0 stores your data differently (database version 3 becomes 6). Follow the [Updating](#updating) steps
above, and keep these five things in mind:

1. **Back up first.** Version 0.1.x has no **Your data** page. Close OpenBerry, quit Claude Desktop, then copy the
   whole `.openberry` folder, with every file in it ([by hand](#back-up-your-data)).
2. **Quit Claude Desktop from the tray**, not only its window: right-click Claude's icon next to the clock and choose
   **Quit** (on a Mac, **Claude → Quit Claude**). Otherwise it keeps the old `openberry-cli` running, and that old
   version must not open your data once 0.2.0 has upgraded it.
3. **The upgrade is automatic.** The first time 0.2.0 opens your data, it saves a copy in `.openberry/backups`
   (named `openberry-pre-v3-…`), then upgrades it. You don't need to do anything else.
4. **There is no downgrade.** Don't open the upgraded data with 0.1.x. To go back, put the copy from step 1 back
   while OpenBerry and Claude Desktop are closed.
5. **Remove the sample company.** If you loaded the demo in 0.1.x, open its company: a banner at the top says it is
   the sample company from the old demo. Click **Delete sample company**. The next page shows what goes and what
   stays, and nothing is removed until you confirm. Its fictional people are never contacted in the meantime.
   `openberry-cli remove-sample` does the same from the command line.

## Closing the app

Closing the window stops OpenBerry (on a Mac, **OpenBerry → Quit OpenBerry** or Cmd+Q also works). Its
scheduled scans, hot-lead alerts and [email sending](EMAIL_SENDING.md) only run while it is open. When you open it again, any scans that have
fallen due run straight away. Claude Desktop can still use OpenBerry's tools while the app is closed.

If OpenBerry opened in your web browser instead of its own window (see below), closing the browser tab does
not stop it: it keeps running in the background. Restart your computer to stop it, or end **OpenBerry** in
Task Manager (Windows: Ctrl+Shift+Esc).

## Uninstall

1. If you want to keep your data, open **Your data** and click **Download backup** first.
2. Close OpenBerry, and quit Claude Desktop.
3. Delete the app: on Windows the `OpenBerry` folder, on a Mac **OpenBerry** in Applications (drag it to the Trash),
   on Linux its folder.
4. Delete the `.openberry` folder in your home folder ([where it is](#your-data-and-settings)). It holds your data
   and your mailbox password.
5. In Claude Desktop, open **Settings → Developer → Edit Config**, remove the `"openberry": { … }` entry from
   `claude_desktop_config.json` (and the comma before it, if there is one), save, and restart Claude Desktop.

## If something goes wrong

- **The app doesn't open, or closes straight away.** Read `.openberry/logs/desktop.log`. The last lines say why.
- **"This data was saved by a newer OpenBerry".** You opened an older version after a newer one updated your data.
  Open the newer version, or [restore a backup](#restore-a-backup) made with this version.
- **"OpenBerry's data file is damaged".** [Restore a backup](#restore-a-backup), for example the newest one in
  `.openberry/backups`. The restore keeps the damaged file there too.
- **"Your disk is full".** OpenBerry can't save anything until there is free space. Free some, then try again.
- **Windows: it opens in the browser instead of a window.** Install the
  [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/).
- **Windows: "Smart App Control blocked an app that may be unsafe".** There is no *Run anyway* button for
  this one. Smart App Control (on some new Windows 11 PCs) only allows signed apps, and OpenBerry isn't signed
  yet. You can turn it off in **Windows Security → App & browser control → Smart App Control settings**; that is
  your decision, and it affects every app.
- **Windows: your antivirus removed `OpenBerry.exe` or `openberry-cli.exe`.** Some antivirus programs distrust
  new, unsigned programs. Restore the file from the antivirus's quarantine and mark it as allowed, or extract
  the zip again.
- **Mac: "OpenBerry is damaged and can't be opened".** The download was probably unzipped by another
  program. Download it again and double-click the zip file in Finder. If that doesn't help, open
  **Terminal** and run `xattr -dr com.apple.quarantine /Applications/OpenBerry.app`.
- **Claude doesn't show the openberry tools.**
  1. Check that the app is still where the settings say.
  2. Copy the settings again from **Connect Claude**.
  3. Quit Claude Desktop completely and reopen it.
  4. On a Mac, if a message says *"openberry-cli" Not Opened* (or can't be opened), allow it the same way as
     the app: **System Settings → Privacy & Security → Open Anyway**, then restart Claude Desktop. Or run the
     `xattr` command from the "damaged" item above, which allows everything inside OpenBerry at once.

  Claude Desktop's own log is under **Settings → Developer → Open Logs Folder**, in `mcp-server-openberry.log`.

## Building it yourself

The app is built with [PyInstaller](https://pyinstaller.org). Build on the system you are building for:
PyInstaller can't make a Windows app on a Mac, for example. You need [uv](https://docs.astral.sh/uv/) and a
copy of this repository.

```bash
uv sync --extra desktop --group build       # on Linux: uv sync --group build (no window there)
uv run --no-sync python packaging/build.py  # build the app and its download
uv run --no-sync python packaging/smoke_test.py dist/OpenBerry/openberry-cli dist/OpenBerry/OpenBerry
```

On a Mac, test `dist/OpenBerry.app/Contents/MacOS/openberry-cli` and `dist/OpenBerry.app/Contents/MacOS/OpenBerry`.
On Windows, add `.exe` to both names.

What the build makes in `dist/`:

| | Windows | Mac | Linux |
|---|---|---|---|
| The app | `OpenBerry/OpenBerry.exe` | `OpenBerry.app` | `OpenBerry/OpenBerry` |
| Command line (Claude Desktop runs `openberry-cli mcp`) | `OpenBerry/openberry-cli.exe` | `OpenBerry.app/Contents/MacOS/openberry-cli` | `OpenBerry/openberry-cli` |
| The download | `OpenBerry-windows-x64.zip` | `OpenBerry-macos-arm64.zip` | `OpenBerry-linux-x64.tar.gz` |
| Licenses | `OpenBerry/LICENSE.txt`, `OpenBerry/THIRD_PARTY_NOTICES.txt` | `LICENSE.txt`, `THIRD_PARTY_NOTICES.txt` next to `OpenBerry.app` in the download (`licenses/` in `dist/`) | as on Windows |

How it fits together:

- **`packaging/openberry.spec`** is the PyInstaller recipe. It builds two executables from one analysis of
  `packaging/launcher.py`, and they share the `_internal` folder:
  - `OpenBerry` has no console.
  - `openberry-cli` has a console, for Claude Desktop's stdio and for every `openberry` command
    (`openberry-cli serve`, `openberry-cli scan`, …).

  Both run `openberry.desktop.main`. On a Mac the spec wraps the folder into `OpenBerry.app`
  (`io.openberry.desktop`), with both executables in `Contents/MacOS`. On Linux it leaves out pywebview.
  It leaves out Python's `readline` and `curses` modules everywhere: on Linux `readline` would bring
  GNU Readline (GPL-3.0) into the download.
- **`packaging/build.py`** runs PyInstaller, then packs the download. On a Mac it uses `ditto`, which keeps
  the app's symlinks and signature. Every download holds `LICENSE.txt` (OpenBerry's MIT license) and
  `THIRD_PARTY_NOTICES.txt`: the licenses of Python, OpenSSL, SQLite, the PyInstaller bootloader, the WebView2
  SDK (Windows) and every Python package the app bundles, read from the build's environment (installed from
  `uv.lock`). On a Mac they sit next to `OpenBerry.app`: files added inside it would break its signature. Its options:
  - `--skip-build` only repacks.
  - `--no-archive` only builds.
  - `--release-tag vX.Y.Z` checks the version.
- **`packaging/smoke_test.py`** checks a build with a temporary data folder:
  1. `--version`
  2. `serve`: health check, then it registers a company and a lead through the JSON API and opens the dashboard,
     the lead's page, the Connect Claude page and a static file.
  3. An MCP handshake over stdio with `tools/list` (25 tools) and a tool call that sees that company.
  4. `OpenBerry --smoke`: the app starts its server, checks it and stops, without a window.

  It exits with an error and says what broke.
- **`packaging/icons/`** holds the app icons. `packaging/make_icons.py` makes them from the dashboard's logo.
- On Linux the unpacked app is about 86 MB and the download about 43 MB.

To run the app from source instead, use `uv run openberry desktop`, after `uv sync --extra desktop` for the window.

## Releasing a new version

Push a tag, and only a tag. Never create the release in GitHub's web page: a release made there has no downloads,
becomes "Latest" at once, and every download link in this guide and the README then answers 404.

1. Set the same new version in `src/openberry/__init__.py` (`__version__`) and in `pyproject.toml`, and run
   `uv lock`. Add a section for it at the top of `CHANGELOG.md` (`## 0.2.0`): what changed, and how to upgrade if
   people need to do anything. It becomes the release notes. Merge the change into `main`.
2. Tag the release and push the tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
3. The **Desktop app** workflow (`.github/workflows/desktop.yml`) then:
   1. Runs the tests and builds the app on Windows, macOS (Apple silicon) and Linux.
   2. Smoke-tests each build.
   3. Creates a draft release named "OpenBerry v0.2.0" with the three downloads and `SHA256SUMS` (their checksums).
      Its notes are the version's section of `CHANGELOG.md`, followed by the list of merged pull requests.
      A draft is invisible to the download links.
   4. Publishes the draft as the latest release, once it holds every file.
   5. Checks that the three `releases/latest/download/...` links answer 200.

   If the tag doesn't match the version in the code, `CHANGELOG.md` has no section for it, or a test or a build
   fails, nothing is published.

Pull requests and pushes to `main` that change the app run the same tests, build and smoke test. Their downloads stay
under the run's **Artifacts** for 14 days.

**If a release goes wrong:**
- *A build or a test failed:* nothing was published. Fix the problem on `main`, then move the tag to the fixed
  commit and push it again: `git tag -f v0.2.0 && git push -f origin v0.2.0`.
- *The workflow stopped at "Create the release as a draft" because the release exists:* someone created it in the
  web page. Delete that release (keep the tag): `gh release delete v0.2.0 -R connexionlimodubai-pixel/gj`, then
  re-run the failed jobs from the workflow run's page. A draft an earlier run left is replaced by itself.
- *"Check the download links" failed:* open the release. If a file is missing, delete the release (keep the tag) and
  re-run the failed jobs. If GitHub was only slow, re-run that job.
- *The download links answer 404 after all:* another release without files is marked "Latest". Delete it
  (`gh release delete <tag> --cleanup-tag`), or open the last good release, choose **Edit** and tick
  **Set as the latest release**.

**Not done yet:**
- Code signing (Windows) and notarisation (macOS). Until then, people see the warnings described above.
- An Intel Mac build. Add an Intel macOS runner (`macos-13`) to the workflow's matrix to make one.
- Automatic updates.
