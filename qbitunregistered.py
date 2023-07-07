#!/usr/bin/python3
import config
from urllib.parse import urlsplit
from qbittorrentapi import Client

client = Client(host=(config.host), username=(config.username), password=(config.password))
unregistered = config.unregistered

torrent_file_paths = {}
for torrent in client.torrents.info():
    if torrent.save_path not in torrent_file_paths:
        torrent_file_paths[torrent.save_path] = [torrent.hash]
    else:
        torrent_file_paths[torrent.save_path].append(torrent.hash)

    unregistered_count = 0
    for tracker in torrent.trackers:
        if tracker.msg in unregistered and tracker.status == 4:
            unregistered_count += 1
            tracker_short = urlsplit(tracker.url)
            print(torrent.name,' ',tracker.msg,' ',tracker_short.netloc)

    if unregistered_count > 0:
        if len(torrent_file_paths[torrent.save_path]) > 1:
            client.torrents_add_tags(tags=["unregistered:crossseeding"], torrent_hashes=[torrent.hash])
        else:
            client.torrents_add_tags(tags=["unregistered"], torrent_hashes=[torrent.hash])
        continue

    for tracker in torrent.trackers:
        if tracker.msg != 'This torrent is private' and tracker.status == 4 and tracker.msg not in unregistered:
            tracker_short = urlsplit(tracker.url)
            print(torrent.name,' ',tracker.msg,' ',tracker_short.netloc)
            client.torrents_add_tags(tags=["issue"],torrent_hashes=[torrent.hash])
