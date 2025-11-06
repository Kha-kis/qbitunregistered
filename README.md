# qbitunregistered

`qbitunregistered` is a powerful Python script for automating and managing a range of tasks in qBittorrent. It's designed to streamline the management of torrents with features for handling orphaned files, unregistered torrents, and more, all customizable through command-line arguments and a configuration file.

## Features

- **Orphaned File Checks**: Detect and report orphaned files to maintain a clean storage environment.

- **Unregistered Checks**: Identify and handle unregistered torrents based on user-defined configurations.
- **Tagging System**: Apply tags to torrents based on tracker source, age, and other criteria for easy organization.
- **Seeding Management**: Implement seed time and seed ratio limits to optimize seeding strategy.
- **Torrent Management**: Control torrent activity with pause, resume, and auto-management functions.
- **Automatic Removal**: Automatically remove torrents that meet specified conditions to manage space and ratio.
- **Hard Link Creation**: Generate hard links for completed downloads for better file management.
- **Dry Run Mode**: Test configurations and script behavior without making actual changes to your setup.


## Prerequisites

- Python 3.9 or newer installed on your system.
- qBittorrent with Web UI access.
- Dependencies from `requirements.txt` installed.

## Installation

Clone the repository and install the required Python packages:

```bash
git clone https://github.com/your-username/qbitunregistered.git
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
