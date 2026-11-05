"""Worker Telegram: scarica documenti dai canali e li pubblica su RabbitMQ."""

from sqlalchemy import text

from telethon.tl.types import Document

import config
from utils import (
    IGNORED_EXTENSIONS_SET,
    DownloadManager,
    TelegramBaseWorker,
    cleanup_stale_downloading,
    download_and_register_document,
    get_db_engine,
    get_publisher,
    load_active_channels,
    setup_logging,
)

environment = get_db_engine()
download_manager = DownloadManager(environment)


class DownloadWorker(TelegramBaseWorker):
    """Scarica documenti Telegram e li registra sul DB + RabbitMQ."""

    def __init__(self):
        super().__init__(
            session=config.SESSION,
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            phone=config.API_PHONE,
            state_table="channel_state",
        )
        self.download_manager = download_manager

    async def process_message(self, msg, publisher) -> None:
        cid = str(msg.chat_id)
        ch_name = self.channel_names.get(cid, cid)
        self.logger.debug("Processing msg_id=%s on %s (ID: %s)", msg.id, ch_name, cid)

        last_id = self.state_manager.get(cid, 0)
        if msg.id <= last_id:
            self.logger.debug(
                "[#%s] Skipping already processed message (last_id=%s) on %s (ID: %s)",
                msg.id,
                last_id,
                ch_name,
                cid,
            )
            return

        if not msg.document or not isinstance(msg.document, Document):
            self.state_manager.save(cid, max(msg.id, last_id))
            return

        success = await download_and_register_document(
            client=self.client,
            msg=msg,
            publisher=publisher,
            download_manager=self.download_manager,
            cid=cid,
            ch_name=ch_name,
            ignored_extensions_set=IGNORED_EXTENSIONS_SET,
            on_skip_callback=lambda: self.state_manager.save(cid, max(msg.id, last_id)),
        )

        if success:
            self.state_manager.save(cid, max(msg.id, last_id))


def main():
    setup_logging()

    cleanup_stale_downloading(download_manager)

    publisher = get_publisher(config.RABBIT_CONNECTION_STRING, config.UNIQUES_QUEUE)

    worker = DownloadWorker()
    worker.run(
        publisher=publisher,
        channels_loader=lambda: load_active_channels(environment),
    )


if __name__ == "__main__":
    main()
