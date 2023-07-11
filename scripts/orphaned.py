import os

def check_files_on_disk(client):
    # Retrieve the save paths from torrent categories
    category_save_paths = [category["savePath"] for category in client.torrents_categories() if category["savePath"]]

    # Retrieve the save path from qBittorrent configuration
    qbit_save_path = client.app.preferences().get("save_path")

    # Combine the save paths from categories and configuration
    save_paths = category_save_paths + [qbit_save_path] if qbit_save_path else category_save_paths

    # Initialize the set to store orphaned files
    orphaned_files = set()

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
                # File is orphaned, add it to the set
                orphaned_files.add(os.path.join(save_path, file))

    # Return the set of orphaned files
    return orphaned_files



def is_orphaned(file, torrents):
    # Check if the file is orphaned based on the list of torrents
    for torrent in torrents:
        if any(file.startswith(content_path) for content_path in torrent.content_path):
            # File is associated with a torrent, not orphaned
            return False
    
    # File is orphaned
    return True


def process_orphaned_file(save_path, file):
    # Perform actions for orphaned file
    # For example, you can print the file path
    print(f"Orphaned file: {os.path.join(save_path, file)}")
