# qbitunregistered

This is a Python script that helps manage torrents in qBittorrent by checking torrent tracker messages. It can add specific tags to torrents based on tracker messages and status, particularly focusing on unregistered torrents.

## Features

- Checks all torrents in qBittorrent for tracker status messages.
- Adds tags to torrents that have unregistered tracker messages.
- Validates if cross-seeding is taking place and tags seperatly
- Customizable through a config file.
- Option for a dry run to see what actions the script will perform without making changes.

## Requirements

- Python 3.x
- qBittorrent with Web UI enabled

## Installation

1. Clone the repository or download the source code.
2. Install the required Python libraries by running: `pip install -r requirements.txt`.

## Configuration

Before running the script, you must configure it by editing the `config.py` file. Below are the options you can set:

- `host`: The host and port where qBittorrent is running.
- `username`: The username for logging into qBittorrent's Web UI.
- `password`: The password for logging into qBittorrent's Web UI.
- `unregistered`: A list of messages or patterns to identify unregistered torrents.
- `dry_run`: A flag for dry run mode. If set to True, the script will only print actions without executing them.
- `other_issues_tag`: The tag to be used for torrents that have issues other than being unregistered.

## Usage

1. Make sure you've configured the script correctly via the `config.py` file.
2. Run the script by executing: `python qbitunregistered.py`
## Contributing

Contributions are welcome! Please feel free to submit a pull request or create issues if you find any.

## License

[MIT License](LICENSE)
