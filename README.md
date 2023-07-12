# qbitunregistered

This is a Python script that helps manage torrents in qBittorrent by checking torrent tracker messages. It can add specific tags to torrents based on tracker messages and status, particularly focusing on unregistered torrents.

## Features

- Checks all torrents in qBittorrent for tracker status messages.
- Adds tags to torrents that have unregistered tracker messages.
- Validates if unregistered torrents are cross-seeding tags and them separately.
- Customizable through a config file.
- Option for a dry run to see what actions the script will perform without making changes.

## Requirements

- Python 3.x
- qBittorrent with Web UI enabled

## Installation

1. Clone the repository or download the source code.
2. Install the required Python libraries by running: `pip install -r requirements.txt`.

## Configuration

Before running the script, you must create a configuration file by copying the provided example configuration file and editing it.

1. Copy the example configuration file: `cp config.json.example config.json`.
2. Edit `config.json` with your own settings.

Below are the options you can set in `config.json`:

- `host`: The host and port where qBittorrent is running.
- `username`: The username for logging into qBittorrent's Web UI.
- `password`: The password for logging into qBittorrent's Web UI.
- `dry_run`: A flag for dry run mode. If set to true, the script will only print actions without executing them. 
- `use_delete_tags`: If set to true, torrents with specified tags will be deleted.
- `use_delete_files`: If set to true, files for torrents with specified tags will be deleted.
- `delete_tags`: list of tags that should trigger the deletion of torrents and/or files.
- `delete_files`: A dictionary specifying whether to delete files for each tag. The keys represent the - `tags, and the values represent whether to delete files (true) or not (false) for each tag.
- `unregistered`: A list of messages or patterns to identify unregistered torrents.

## Usage

1. Make sure you've configured the script correctly via the `config.py` file.
2. Run the script by executing: `python qbitunregistered.py`.

You can also override configuration settings with command-line arguments:

```sh
python python qbitunregistered.py --config config.json --orphaned --unregistered --dry-run --host "localhost:8080" --username "admin" --password "password"
```

Available command-line arguments:

- `--config`: Path to the configuration file (default: config.json).
- `--orphaned`: If set, check for orphaned files on disk.
- `--unregistered`: If set, perform unregistered checks.
- `--dry-run`: If set, the script will only print actions without executing them.
- `--host`: Override the host and port where qBittorrent is running.
- `--username`: Override the username for logging into qBittorrent's Web UI.
- `--password`: Override the password for logging into qBittorrent's Web UI.

## Contributing

Contributions are welcome! Please feel free to submit a pull request or create issues if you find any.

## License

[MIT License](LICENSE)

Please let me know if there are any further updates or modifications needed.