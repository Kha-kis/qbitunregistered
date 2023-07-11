import os

def check_files_on_disk(client):
    # Retrieve the save paths from qBittorrent
    save_paths = client.preferences.save_path

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
        if torrent.content_path == file:
            # File is associated with a torrent, not orphaned
            return False
    
    # File is orphaned
    return True

def process_orphaned_file(save_path, file):
    # Perform actions for orphaned file
    # For example, you can print the file path
    print(f"Orphaned file: {os.path.join(save_path, file)}")

# Example usage
if __name__ == "__main__":
    # Connect to qBittorrent
    client = Client("http://localhost:8080/")

    # Set the API credentials if needed
    # client.login("username", "password")

    # Call the check_files_on_disk function
    check_files_on_disk(client)
