# qbitunregistered

`qbitunregistered` is a powerful Python script for automating and managing a range of tasks in qBittorrent. It's designed to streamline the management of torrents with features for handling orphaned files, unregistered torrents, and more, all customizable through command-line arguments and a configuration file.

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

- Python 3.9 or newer installed on your system.
- qBittorrent with Web UI access.
- Dependencies from `requirements.txt` installed.

## Installation

Clone the repository and install the required Python packages:

```bash
git clone https://github.com/Kha-kis/qbitunregistered.git
cd qbitunregistered
pip install -r requirements.txt
```

## Upgrading

### From Pre-3.9 Python Versions

**Important**: This version requires Python 3.9 or newer due to the use of `Path.is_relative_to()` and other modern Python features.

If you're upgrading from an older version:

1. **Check your Python version:**
   ```bash
   python3 --version
   ```

2. **If you're on Python 3.8 or older, upgrade Python first:**
   - Ubuntu/Debian: `sudo apt update && sudo apt install python3.9`
   - macOS (Homebrew): `brew install python@3.9`
   - Windows: Download from [python.org](https://www.python.org/downloads/)

3. **Update dependencies:**
   ```bash
   pip install -r requirements.txt --upgrade
   ```

4. **Key Changes in This Version:**
   - **Minimum Python**: 3.9+ (was 3.6+)
   - **New dependency**: tqdm >=4.66.3 (for progress bars and security fix)
   - **Breaking change**: Path handling now uses Python 3.9+ features
   - **Performance**: Major improvements through API call batching (4000+ → 15-20 calls)
   - **New features**: Caching layer, progress bars, improved error handling

## Configuration

Start by copying the example configuration file and then modify it to suit your needs:

```bash
cp config.json.example config.json
```

Edit `config.json` with your preferred text editor, and set your qBittorrent credentials, preferred behaviors, and other settings.

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
0 2 * * * cd /path/to/qbitunregistered && /usr/bin/python3 qbitunregistered.py --unregistered --log-file /var/log/qbitunregistered.log
```

**Best Practices:**
- Never commit `config.json` to version control (already in `.gitignore`)
- Use environment variables for credentials in CI/CD environments
- Rotate passwords periodically
- Consider using qBittorrent's IP whitelist feature to restrict Web API access

## Usage

Execute the script with Python, appending any command-line arguments you wish to use:

```bash
python qbitunregistered.py --option1 --option2
```

### Command-Line Arguments

Here's what you can specify when running `qbitunregistered`:

- `--config`: Custom path to your configuration file.
- `--orphaned`: Activate orphaned file checking.
- `--unregistered`: Enable checks for unregistered torrents.
- `--dry-run`: Simulate script actions without making changes.
- `--host`: Specify the host and port where qBittorrent is running.
- `--username`: Your username for logging into the qBittorrent Web UI.
- `--password`: Your password for logging into the qBittorrent Web UI.
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
- `--yes`, `-y`: Skip confirmation prompt and proceed with operations automatically. Use with caution! Recommended for automation/cron jobs after testing with dry-run.

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
- This prevents overwriting previously recycled files
- Useful when the same file is deleted multiple times

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

When deleting unregistered torrents with `delete_files=True`:

**With Recycle Bin Configured:**
1. Script gets all file paths for the torrent
2. Files are moved to `/recycle_bin/unregistered/{category}/[paths]`
3. Torrent is deleted from qBittorrent WITHOUT deleting files
4. Result: Files safely preserved in organized recycle bin, torrent removed

**Without Recycle Bin:**
1. Torrent is deleted with `delete_files=True`
2. qBittorrent permanently deletes both torrent and files
3. Result: Permanent deletion (original behavior)

**Important Notes:**
- Cross-seeded torrents are handled intelligently
- Files are only moved once, even if multiple torrents reference them
- If file paths cannot be retrieved, torrent is still deleted (logged as warning)

### Example Usage

```bash
# Test with dry-run first (see what would be recycled)
python qbitunregistered.py --orphaned --unregistered --dry-run

# Run orphaned check with recycle bin
python qbitunregistered.py --orphaned

# Run unregistered check with recycle bin
python qbitunregistered.py --unregistered

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
- `notifiarr_key` must be a non-empty string
- `notifiarr_channel` must be a valid Discord channel ID:
  - Provided as a string
  - Contains only digits
  - Length between 17 and 20 characters (inclusive)

**Features:**
- Color-coded notifications (green for success, red for failures)
- Automatic retry with exponential backoff (3 attempts max)
- Discord character limit handling (2000 chars, auto-truncation)
- Credential sanitization in error logs (API keys are redacted if returned in error bodies)

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
