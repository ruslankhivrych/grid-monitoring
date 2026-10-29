# Windows monitoring host

The Windows deployment runs without an interactive login or Docker Desktop.
Each application has its own virtual environment, configuration, data, logs,
backup directory and named scheduled tasks.

```text
C:\Monitoring\
  apps\grid-monitoring\       application sources and .venv
    bot\.env                  Telegram and FSolar credentials
  data\grid-monitoring\       bot.db and matplotlib cache
  logs\grid-monitoring\       bot.log, maximum 5 MB plus 5 rotated copies
  backups\grid-monitoring\    14 daily automatic SQLite backups
  runtime\Python313\          shared base interpreter
```

`\Monitoring\grid-monitoring` runs under the built-in LOCAL SERVICE account
(`S-1-5-19`), without a user password and without administrator privileges.
It starts at boot, retries failures after one minute and has a five-minute
repeated trigger as a fallback. Running instances are not duplicated. There is
no execution time limit, and switching to battery does not stop the task.

`\Monitoring\grid-monitoring-backup` runs daily at 03:15 in the Windows time
zone. It uses SQLite's online backup API and retains 14 automatic copies.
Manual and migration backups are preserved. This is a local backup; an offline
copy is needed to recover from loss of the laptop or its disk.

## Initial installation

1. Enable SSH with `scripts/enable-windows-ssh.ps1` from an elevated local console.
2. Install a supported 64-bit Python into `C:\Monitoring\runtime\Python313`.
   Verify the official installer signature before running it. No system PATH
   change is required.
3. Copy this project to `C:\Monitoring\apps\grid-monitoring`. Run the following
   preparation command as administrator before copying secrets or a database:

   ```powershell
   powershell -File deploy\windows\install-task.ps1 -PrepareOnly
   ```

4. Put the real configuration into `bot\.env` and any recovered database into
   `C:\Monitoring\data\grid-monitoring\bot.db`. Set `ALLOWED_CHAT_ID` explicitly.
   Preserve a separate copy of the recovered database before starting the bot.
5. Create the environment and install dependencies:

   ```powershell
   C:\Monitoring\runtime\Python313\python.exe -m venv .venv
   .venv\Scripts\python.exe -m pip install -r deploy\windows\requirements.txt
   .venv\Scripts\python.exe -m pip check
   ```

6. Run `deploy\windows\install-task.ps1` as administrator. It validates the
   configuration, sets project permissions, registers both tasks and starts
   the bot. The launcher always uses the separate native data directory,
   regardless of a Docker/Linux `DB_PATH` saved in `.env`.
7. Disable idle sleep and hibernation on AC and battery while hosting monitors.
   If supported, set closing the lid to do nothing. Keep the Windows critical
   battery action enabled. Power settings should be backed up before changing
   them. Returning to this deployment does not require restarting the laptop.

Protected directories grant administrators and SYSTEM full control. LOCAL
SERVICE can read application code/configuration and modify its data, logs and
backups. All monitors using this same service account share its identity;
use separate service accounts if stronger isolation is required later.

## Operation

Run management commands from an elevated PowerShell:

```powershell
Get-ScheduledTask -TaskPath '\Monitoring\'
Get-ScheduledTaskInfo -TaskPath '\Monitoring\' -TaskName grid-monitoring
Get-Content C:\Monitoring\logs\grid-monitoring\bot.log -Tail 40
Stop-ScheduledTask -TaskPath '\Monitoring\' -TaskName grid-monitoring
Start-ScheduledTask -TaskPath '\Monitoring\' -TaskName grid-monitoring
Start-ScheduledTask -TaskPath '\Monitoring\' -TaskName grid-monitoring-backup
```

To leave the bot stopped for maintenance, disable its task as well as stopping
it, because the repeated trigger otherwise starts it again. Enable the task
when maintenance is finished.

After updating `bot\.env`, restart the bot task. Stop/disable the task before
replacing code or restoring a database. Retain the `.env`, `.venv`, data, logs
and backups. Never run another polling instance using the same Telegram token.

Other monitors can use sibling directories under `apps`, `data`, `logs` and
`backups`, their own environments and distinct tasks in `\Monitoring\`.
The included launcher/install script is specific to this Telegram application.

Task Scheduler resumes the bot after Windows boots. Automatic hardware power-on
after a complete shutdown or battery depletion depends on the laptop firmware.
