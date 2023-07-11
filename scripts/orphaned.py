import os

def get_root_files(directory):
    root_files = []
    for root, _, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            root_files.append(file_path)
    return root_files
