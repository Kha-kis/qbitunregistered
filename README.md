# qbitunregistered
This is a simple script to allow deletion of unregistered torrents in QbitTorrent

Install requirements

```pip install -r requirements.txt```

Config:

This is found in the config.py file

It is used to define the following parameters:

host 
username
password
delete_files

Host, Username, and Password are specific to your Qbittorrent client

The delete files option is if you would like to remove files from disk.

TO-DO:

Add a dry run option.

When delete_files option is enabled identification of torrents that are cross seeded and removing just the torrent for these and not the files.
