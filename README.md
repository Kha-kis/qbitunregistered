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

1. Copy the example configuration file: `cp config.py.example config.py`.
2. Edit `config.py` with your own settings.

Below are the options you can set in `config.py`:

- `host`: The host and port where qBittorrent is running.
- `username`: The username for logging into qBittorrent's Web UI.
- `password`: The password for logging into qBittorrent's Web UI.
- `unregistered`: A list of messages or patterns to identify unregistered torrents.
- `dry_run`: A flag for dry run mode. If set to True, the script will only print actions without executing them.
- `other_issues_tag`: The tag to be used for torrents that have issues other than being unregistered.

## Usage

1. Make sure you've configured the script correctly via the `config.py` file.
2. Run the script by executing: `python qbitunregistered.py`.

You can also override configuration settings with command-line arguments:

`python qbitunregistered.py --host "localhost:8080" --username "admin" --password "password" --dry-run --other-issues-tag "other_issues"`

Available command-line arguments:

- `--host`: Override the host and port where qBittorrent is running.
- `--username`: Override the username for logging into qBittorrent's Web UI.
- `--password`: Override the password for logging into qBittorrent's Web UI.
- `--dry-run`: If set, the script will only print actions without executing them.
- `--other-issues-tag`: Override the tag to be used for torrents that have issues other than being unregistered.

## Contributing

Contributions are welcome! Please feel free to submit a pull request or create issues if you find any.

## License

[MIT License](LICENSE)
