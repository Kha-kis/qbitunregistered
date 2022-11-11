# qbitunregistered
This is a simple script to tag unregistered torrents in QbitTorrent.
Other messages whhere a torrent is in a not working status are also tagged with a tag named from the message

Install requirements

```pip install -r requirements.txt```

Config:

copy the config.py.example to config.py
Update the needed paramaters for your qbittorrent client.

config.py is used to define the following parameters:

host 
username
password
tagname

Host, Username, and Password are specific to your Qbittorrent client

tagname is the tag you would like use in qbittorrent

TO-DO:

Add a dry run option.

Identification of torrents that are cross seeded and applying a different tag.
