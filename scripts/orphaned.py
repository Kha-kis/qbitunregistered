import os

def check_files_on_disk(client):
    # Retrieve the save paths from qBittorrent
    save_paths = [save_path.path for save_path in client.torrents.save_path()]

    # Iterate over the save paths and check for orphaned files
    for save_path in save_paths:
        # Get the list of files/directories in the save path
        files_on_disk = os.listdir(save_path)

        # Fetch the list of torrents from qBittorrent
        torrents = client.torrents.info()

        # Check each file in the save path
        for file in files_on_disk:
            # Check if the file is orphaned
            if is_orphaned(file, torrents):
                # File is orphaned, perform actions
                process_orphaned_file(save_path, file)

def is_orphaned(file, torrents):
    # Check if the file is orphaned based on the list of torrents
    for torrent in torrents:
        if torrent.info.file_name == file:
            # File is associated with a torrent, not orphaned
            return False
    
    # File is orphaned
    return True

def process_orphaned_file(save_path, file):
    # Perform actions for orphaned file
    # For example, you can print the file path
    print(f"Orphaned file: {os.path.join(save_path, file)}")
