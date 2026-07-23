# qbitunregistered

`qbitunregistered` automates common qBittorrent maintenance tasks, including orphan cleanup, unregistered torrent handling, tagging, seeding limits, and notifications.

## Features

- **Orphaned File Checks**: Detect and report orphaned files to maintain a clean storage environment.
- **Recycle Bin**: Safely move orphaned files to a recycle bin instead of permanent deletion, with automatic collision handling.
- **Unregistered Checks**: Identify and handle unregistered torrents based on user-defined configurations.
- **Tagging System**: Apply tags to torrents based on tracker source, age, and other criteria for easy organization.
- **Seeding Management**: Implement seed time and seed ratio limits to optimize seeding strategy.
- **Torrent Management**: Control torrent activity with pause, resume, and auto-management functions.
- **Automatic Removal**: Automatically remove torrents that meet specified conditions to manage space and ratio.
- **Hard Link Creation**: Generate hard links for completed downloads for better file management.
- **Notifications**: Send operation summaries via Apprise or Notifiarr with automatic retry logic.
- **Dry Run Mode**: Test configurations and script behavior without making actual changes to your setup.


## Prerequisites

- Python 3.11 or newer installed on your system.
- qBittorrent with Web UI access.

## Installation

Clone the repository, create a virtual environment, and install the application:

```bash
git clone https://github.com/Kha-kis/qbitunregistered.git
cd qbitunregistered
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

This installs the `qbitunregistered` and `qbitunregistered-scheduler` commands.

## Upgrading

### From Older Python Versions

**Important**: This version requires Python 3.11 or newer to support current qBittorrent API clients.

If you're upgrading from an older version:

1. **Check your Python version:**
   ```bash
   python3 --version
   ```

2. **If you're on Python 3.10 or older, upgrade Python first:**
   - Ubuntu/Debian: install Python 3.11 or newer from your distribution
   - macOS (Homebrew): `brew install python@3.11`
   - Windows: Download from [python.org](https://www.python.org/downloads/)

3. **Reinstall the application:**
   ```bash
   python -m pip install --upgrade .
   ```

4. **Key Changes in This Version:**
   - **Minimum Python**: 3.11+
   - **New dependency**: tqdm >=4.66.3 (for progress bars and security fix)
   - **Breaking change**: Python 3.9 and 3.10 are no longer supported
   - **Performance**: Major improvements through API call batching (4000+ → 15-20 calls)
   - **New features**: Caching layer, progress bars, improved error handling

## Configuration

Start by copying the example configuration file and then modify it to suit your needs:

```bash
cp config.json.example config.json
```

Edit `config.json` with your preferred text editor, and set your qBittorrent credentials, preferred behaviors, and other settings.

For qBittorrent v5.2.0 or newer, you may authenticate with an API key instead
of a username and password:

```json
{
  "host": "localhost:8080",
  "api_key": "qbt_your_api_key"
}
```

If `api_key` is omitted or blank, `username` and `password` are used.

### Customizable Tags for Unregistered Torrents

The latest update introduces two new configurable tags in `config.json`:

- `default_unregistered_tag`: The tag applied to torrents identified as unregistered. Default is "unregistered".
- `cross_seeding_tag`: Used for torrents that are unregistered but cross-seeding. Default is "unregistered:crossseeding".

These can be customized to align with your tagging strategy, providing enhanced flexibility in torrent management.

### Logging Configuration

Control logging behavior through `config.json` or command-line arguments:

- **`log_level`**: Set logging verbosity (DEBUG, INFO, WARNING, ERROR). Default: INFO
  ```json
  {
    "log_level": "DEBUG"
  }
  ```
  CLI override: `--log-level DEBUG`

- **`log_file`**: Write logs to a file (useful for scheduled/cron runs)
  ```json
  {
    "log_file": "/var/log/qbitunregistered.log"
  }
  ```
  CLI override: `--log-file /path/to/logfile.log`

## Security

### Config File Permissions

Your `config.json` contains sensitive credentials (qBittorrent username and password). Follow these security best practices:

**Linux/macOS:**
```bash
# Set restrictive permissions (owner read/write only)
chmod 600 config.json

# Verify permissions
ls -l config.json
# Should show: -rw------- (only owner can read/write)
```

**Scheduled/Cron Jobs:**

If running via cron, ensure the cron user has appropriate access:
```bash
# Set ownership to the cron user
sudo chown cronuser:cronuser config.json

# Set restrictive permissions
chmod 600 config.json

# Example cron entry (runs daily at 2 AM)
0 2 * * * /path/to/.venv/bin/qbitunregistered --config /path/to/config.json --unregistered --yes --log-file /var/log/qbitunregistered.log
```

**Best Practices:**
- Never commit `config.json` to version control (already in `.gitignore`)
- Use environment variables for credentials in CI/CD environments
- Rotate passwords periodically
- Consider using qBittorrent's IP whitelist feature to restrict Web API access

## Usage

Run the installed command with the operations you want:

```bash
qbitunregistered --config config.json --unregistered --dry-run
```

Use `python -m qbitunregistered` if you prefer module execution. New
installations and automation should use one of these two supported entry
points instead of the legacy root scripts.

To run the built-in scheduler, set both `scheduled_times` and
`scheduled_operations` in the configuration, then pass that same file to the
scheduler:

```json
{
  "scheduled_times": ["09:00", "21:00"],
  "scheduled_operations": ["unregistered", "orphaned"]
}
```

```bash
qbitunregistered-scheduler --config /absolute/path/to/config.json
```

Scheduled runs forward those operation flags and add `--yes` automatically.
The scheduler rejects configured times with no operations. Test the same
operations with `"dry_run": true` before enabling real scheduled mutations.

### Legacy source-checkout commands

The following commands were documented before the project became an
installable package:

```bash
python qbitunregistered.py --config config.json --unregistered
python scheduler.py
```

They remain supported throughout the 2.x series so existing cron jobs and
source-checkout workflows do not break. Both root scripts are deprecated and
planned for removal in 3.0. Migrate new and existing automation to
`qbitunregistered` and `qbitunregistered-scheduler`. The legacy `scheduler.py`
wrapper continues to find `config.json` beside the script rather than in the
current working directory.

### Command-Line Arguments

Here's what you can specify when running `qbitunregistered`:

- `--config`: Custom path to your configuration file.
- `--orphaned`: Activate orphaned file checking.
- `--unregistered`: Enable checks for unregistered torrents.
- `--dry-run` / `--no-dry-run`: Override dry-run mode from the configuration file.
- `--host`: Specify the host and port where qBittorrent is running.
- `--username`: Your username for logging into the qBittorrent Web UI.
- `--password`: Your password for logging into the qBittorrent Web UI.
- `--api-key`: API key for qBittorrent v5.2.0 or newer. A non-blank value takes precedence over username/password; an explicitly blank value selects username/password fallback.
- `--tag-by-tracker`: Perform tagging based on the associated tracker.
- `--seeding-management`: Apply seed time and seed ratio limits based on tracker tags.
- `--auto-tmm`: Enable Automatic Torrent Management (auto TMM).
- `--pause-torrents`: Pause all torrents.
- `--resume-torrents`: Resume all torrents.
- `--auto-remove`: Automatically remove completed torrents.
- `--create-hard-links`: Create hard links for completed torrents in the target directory.
- `--target-dir`: Specify the target directory for organizing completed torrents.
- `--tag-by-age`: Perform tagging based on torrent age in months.
- `--exclude-files`: Exclude files from being considered in operations based on glob patterns (e.g., `*.tmp`, `*.part`). Multiple patterns can be specified separated by spaces.
- `--exclude-dirs`: Exclude directories from being scanned for orphaned files. Full paths should be specified, and wildcards can be used to match multiple directories (e.g., `/path/to/exclude/*`). Multiple paths can be specified separated by spaces.
- `--log-level`: Set logging verbosity (DEBUG, INFO, WARNING, ERROR). Overrides config.json setting.
- `--log-file`: Write logs to specified file in addition to console. Useful for scheduled/cron runs.
- `--yes`, `-y`: Skip impact analysis and confirmation and proceed with operations automatically. Use with caution and only after testing the same operation set with dry-run.

Without `--yes`, every selected non-dry-run operation is previewed and requires
confirmation. If any target cannot be analyzed reliably, execution aborts
before mutation. Orphaned-file targets and hard-link destinations shown in the
preview are reused for execution so the confirmed list is the list processed.
Orphan plans also bind each path to its device, inode, type, size, and
modification time; missing, modified, substituted, or symlinked targets are
preserved. Unregistered previews distinguish torrent-only deletion,
cross-seeded-file preservation, recycling, and permanent deletion. Before
file mutation, execution refreshes qBittorrent's ownership state without the
preview cache and aborts if it changed.

## Recycle Bin Feature

The recycle bin feature provides a safer alternative to permanent deletion for both orphaned files and unregistered torrent deletions. When enabled, files are moved to an organized recycle bin directory instead of being permanently deleted, allowing for easy recovery if needed.

### Configuration

Add the `recycle_bin` path to your `config.json`:

```json
{
  "recycle_bin": "/path/to/recycle/bin"
}
```

### What Gets Recycled?

**✅ Orphaned Files** (from `--orphaned` operation)
- Files detected by orphan scanning that aren't tracked by any torrent
- Organized in: `/recycle_bin/orphaned/uncategorized/[original_path]`

**✅ Unregistered Torrent Files** (from `--unregistered` operation)
- When unregistered torrents are deleted with `delete_files=True`
- Organized in: `/recycle_bin/unregistered/{category}/[original_path]`
- Category is taken from the torrent's qBittorrent category

**❌ Not Recycled:**
- Torrent-only deletions (when `delete_files=False`)
- Auto-removed torrents (uses qBittorrent's built-in deletion)
- Hard link operations

### Directory Structure (Hybrid Organization)

The recycle bin uses a **hybrid structure** combining deletion type and category:

```
/recycle_bin/
  ├── orphaned/              # Files from orphan scanning
  │   └── uncategorized/     # Orphaned files have no category
  │       └── [original full path structure]
  │           ├── mnt/torrents/movies/movie.mkv
  │           └── var/media/file.mkv
  │
  └── unregistered/          # Files from unregistered torrents
      ├── movies/            # Organized by torrent category
      │   └── [original full path structure]
      ├── tv/
      │   └── [original full path structure]
      └── uncategorized/     # Torrents without a category
          └── [original full path structure]
```

**Benefits of This Structure:**
- **Easy identification**: Instantly know why a file was deleted
- **Category organization**: Browse by content type (movies, tv, etc.)
- **Safe recovery**: Preserved path structure makes restoration simple
- **Audit trail**: Track deletion patterns by type and category

### Behavior Details

**Path Preservation:**
- The original absolute directory structure is maintained within each category
- Example (Unix): `/mnt/torrents/movies/file.mkv` → `/recycle_bin/orphaned/uncategorized/mnt/torrents/movies/file.mkv`
- Example (Unregistered): Category "movies", file at `/data/Movie.mkv` → `/recycle_bin/unregistered/movies/data/Movie.mkv`

**Windows Path Handling:**
- Drive letters are converted to directory names (colon replaced with underscore)
- Example: `C:\Torrents\file.mkv` → `C:\recycle_bin\unregistered\movies\C_\Torrents\file.mkv`
- This ensures cross-platform compatibility and prevents path conflicts

**File Collision Handling:**
- If a file with the same name already exists in the recycle bin, a timestamp suffix is automatically added
- Format: `filename_YYYYMMDD_HHMMSS.ext`
- Example: `movie.mkv` → `movie_20250123_143045.mkv`
- Repeated collisions in the same second add a numeric suffix and never overwrite
  an existing recycled file
- This prevents overwriting previously recycled files
- Useful when the same file is deleted multiple times
- If qBittorrent cannot remove an unregistered torrent after its files are
  recycled, the application restores those files without overwriting any path
  created concurrently.

**Automatic Exclusion:**
- The recycle bin directory is automatically excluded from orphan scanning
- This prevents recycled files from being detected as orphaned again
- No manual configuration needed for this exclusion

**Validation:**
- The recycle bin path must be an absolute path
- Write permissions are validated at startup
- Directory is created automatically if it doesn't exist
- Invalid recycle bin configuration causes startup failure with clear error messages

**Dry-Run Support:**
- In dry-run mode, the script reports what would be moved without actually moving files
- Shows the exact destination path including type and category
- Use this to verify behavior before enabling actual file operations

### Unregistered Torrent Handling

File removal for unregistered torrents requires all three controls:
`use_delete_tags: true`, an exact tag in `delete_tags`, and
`use_delete_files: true` with a boolean `true` value for that tag in
`delete_files`. qBittorrent's comma-separated tags are matched exactly, not as
substrings.

When those controls authorize file removal:

**With Recycle Bin Configured:**
1. Script gets all file paths for the torrent
2. Files are moved to `/recycle_bin/unregistered/{category}/[paths]`
3. Torrent is deleted from qBittorrent WITHOUT deleting files
4. Result: Files safely preserved in organized recycle bin, torrent removed

**Without Recycle Bin:**
1. The script resolves the torrent's files and scans all other torrents for shared ownership
2. Only after that complete scan succeeds is the torrent deleted with `delete_files=True`
3. qBittorrent permanently deletes both torrent and files

**Important Notes:**
- Cross-seeded files are preserved and the torrent is removed without deleting files
- A failed, incomplete, or malformed file/ownership check aborts deletion of that torrent
- Missing files are treated as an unsafe state, not as an empty torrent
- The same ownership checks protect recycle-bin and permanent-deletion modes
- If any file for one unregistered torrent fails to move, earlier moves for that
  torrent are rolled back and the torrent is preserved

### Example Usage

```bash
# Test with dry-run first (see what would be recycled)
qbitunregistered --orphaned --unregistered --dry-run

# Run orphaned check with recycle bin
qbitunregistered --orphaned

# Run unregistered check with recycle bin
qbitunregistered --unregistered

# Browse recycle bin structure
ls -R /path/to/recycle/bin

# Example output:
# /path/to/recycle/bin/
#   orphaned/uncategorized/mnt/downloads/old_file.mkv
#   unregistered/movies/data/Movie_2023.mkv
#   unregistered/tv/media/Show_S01E01.mkv
```

### Restoring Files

To restore a file from the recycle bin:

```bash
# Find the file
find /path/to/recycle/bin -name "movie.mkv"

# Restore to original location (example)
# If file was at: /recycle_bin/unregistered/movies/mnt/data/movie.mkv
# Restore to: /mnt/data/movie.mkv
mv /path/to/recycle/bin/unregistered/movies/mnt/data/movie.mkv /mnt/data/
```

### Managing Recycle Bin Size

The recycle bin will grow over time. Consider:

**Manual Cleanup:**
```bash
# Delete files older than 30 days
find /path/to/recycle/bin -type f -mtime +30 -delete

# Delete empty directories
find /path/to/recycle/bin -type d -empty -delete
```

**Automated Cleanup (Cron):**
```bash
# Add to crontab (runs weekly)
0 2 * * 0 find /path/to/recycle/bin -type f -mtime +30 -delete
```

**Monitor Size:**
```bash
# Check recycle bin size
du -sh /path/to/recycle/bin

# Show breakdown by type
du -sh /path/to/recycle/bin/*/
```

## Notification System

qbitunregistered supports sending operation summaries via notifications using Apprise or Notifiarr.

### Apprise Integration

Configure any Apprise-supported service using a single URL:

```json
{
  "apprise_url": "discord://webhook_id/webhook_token"
}
```

Apprise supports 80+ notification services including Discord, Slack, Telegram, Email, and more. See [Apprise documentation](https://github.com/caronc/apprise) for URL formats.

### Notifiarr Integration

Configure Notifiarr for Discord notifications with custom formatting:

```json
{
  "notifiarr_key": "your-api-key-here",
  "notifiarr_channel": "1234567890123456789"
}
```

**Requirements:**
- Both `notifiarr_key` and `notifiarr_channel` must be provided together
- Channel ID must be a valid Discord channel ID (17-20 digits)

**Features:**
- Color-coded notifications (green for success, red for failures)
- Automatic retry with exponential backoff (3 attempts max)
- Discord character limit handling (2000 chars, auto-truncation)
- Credential sanitization in error logs

### Notification Content

Notifications include:
- Operation summary (succeeded/failed counts)
- List of completed operations
- List of failed operations (if any)

Example notification:
```
qbitunregistered Summary

✅ Succeeded: 3
  - Orphaned files check: 5 files processed
  - Unregistered checks
  - Tag by tracker

❌ Failed: 0
```

## Troubleshooting

If you encounter issues, check the following:

- Ensure qBittorrent is running and accessible.
- Verify that all required Python packages are installed.
- Check the log output for errors and consult the FAQ.

## Frequently Asked Questions

**Q: How often should I run the script?**
**A:** It depends on your needs. Some users run it daily, while others prefer multiple times a day for more active torrent management.

**Q: Can I run this script on a schedule?**
**A:** Yes, you can use cron jobs (Linux/Mac) or Task Scheduler (Windows) to run the script at regular intervals.

## Contributing

Your contributions make this project better! Feel free to report bugs, suggest features, or submit pull requests. For major changes, please open an issue first to discuss what you'd like to change.

## License

This project is released under the MIT License. See the LICENSE file for more details.

## Acknowledgements

Thanks to the qBittorrent team and all contributors to the `qbittorrent-api` and related libraries.

## Contact

For questions, suggestions, or collaboration, please open an issue in the GitHub repository.
