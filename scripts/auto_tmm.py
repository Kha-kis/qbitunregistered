from qbittorrentapi import Client
import logging

def apply_auto_tmm(client, config):
    auto_tmm_enabled = config.get('auto_tmm_enabled')
    torrent_changed_tmm_enabled = config.get('torrent_changed_tmm_enabled')
    save_path_changed_tmm_enabled = config.get('save_path_changed_tmm_enabled')
    category_changed_tmm_enabled = config.get('category_changed_tmm_enabled')

    if auto_tmm_enabled:
        client.preferences_set(
            auto_tmm_enabled=True,
            torrent_changed_tmm_enabled=torrent_changed_tmm_enabled,
            save_path_changed_tmm_enabled=save_path_changed_tmm_enabled,
            category_changed_tmm_enabled=category_changed_tmm_enabled
        )
        logging.info("Enabled Automatic Torrent Management (auto TMM).")