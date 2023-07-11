#!/usr/bin/python3
import config
import argparse
from qbittorrentapi import Client
import logging
from scripts.orphaned import check_files_on_disk
from scripts.unregistered_checks import check_unregistered_torrents

def parse_arguments():
    parser = argparse.ArgumentParser(description="Manage torrents in qBittorrent by checking torrent tracker messages.")
    parser.add_argument('--host', type=str, help='The host and port where qBittorrent is running.')
    parser.add_argument('--username', type=str, help='The username for logging into qBittorrent Web UI.')
    parser.add_argument('--password', type=str, help='The password for logging into qBittorrent Web UI.')
    parser.add_argument('--dry-run', action='store_true', help='If set, the script will only print actions without executing them.')
    parser.add_argument('--other-issues-tag', type=str, help='The tag to be used for torrents that have issues other than being unregistered.')
    parser.add_argument('--use-delete-tags', type=bool, help='Flag for using delete_tags in the script.')
    parser.add_argument('--use-delete-files', type=bool, help='Flag for using delete_files in the script.')
    parser.add_argument('--orphaned', action='store_true', help='If set, check for orphaned files on disk.')
    parser.add_argument('--unregistered', action='store_true', help='If set, checks for unregistered torrents.')
    return parser.parse_args()

def configure_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

def connect_to_qbittorrent(host, username, password):
    return Client(host=host, username=username, password=password)

def fetch_torrent_information(client):
    return client.torrents.info()

def process_unregistered_torrents(client, dry_run):
    unregistered_counts_per_path = check_unregistered_torrents(client)
    if config.use_delete_tags:
        delete_tags = config.delete_tags
        delete_files = config.delete_files
        for torrent in client.torrents.info():
            for tag in delete_tags:
                if tag in torrent.tags:
                    if config.use_delete_files and delete_files.get(tag, False):
                        if not dry_run:
                            client.torrents.delete(torrent.hash, delete_files=True)
                            logging.info("Deleted torrent '%s' with hash %s and its files.", torrent.name, torrent.hash)
                        else:
                            logging.info("[Dry Run] Would delete torrent '%s' with hash %s and its files.", torrent.name, torrent.hash)
                    else:
                        if not dry_run:
                            client.torrents.delete(torrent.hash, delete_files=False)
                            logging.info("Deleted torrent '%s' with hash %s.", torrent.name, torrent.hash)
                        else:
                            logging.info("[Dry Run] Would delete torrent '%s' with hash %s.", torrent.name, torrent.hash)
    return unregistered_counts_per_path

def process_orphaned_files(client):
    check_files_on_disk(client)

def main():
    args = parse_arguments()
    host = args.host if args.host else config.host
    username = args.username if args.username else config.username
    password = args.password if args.password else config.password
    dry_run = args.dry_run if args.dry_run else config.dry_run
    
    configure_logging()
    
    client = connect_to_qbittorrent(host, username, password)
    logging.info("Starting qbitunregistered script...")
    
    torrents = fetch_torrent_information(client)
    logging.info("Total torrents found: %d", len(torrents))
    
    if args.unregistered:
        unregistered_counts_per_path = process_unregistered_torrents(client, dry_run)
    
    if args.orphaned:
        process_orphaned_files(client)
    
    logging.info("qbitunregistered script completed.")

if __name__ == '__main__':
    main()