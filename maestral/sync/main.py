# !/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Oct 31 16:23:13 2018

@author: samschott
"""

# system imports
import sys
import os
import os.path as osp
import shutil
import time
import functools
from threading import Thread
import logging.handlers

# external packages
from dropbox import files
from blinker import signal
import requests

# maestral modules
from maestral.sync.client import MaestralApiClient, dropbox_stone_to_dict, remove_tags
from maestral.sync.oauth import OAuth2Session
from maestral.sync.errors import (MaestralApiError, CONNECTION_ERRORS, DropboxAuthError,
                                  CONNECTION_ERROR_MSG)
from maestral.sync.monitor import (MaestralMonitor, IDLE, DISCONNECTED,
                                   path_exists_case_insensitive, is_child)
from maestral.config.main import CONF
from maestral.config.base import get_home_dir
from maestral.sync.utils.app_dirs import get_log_path, get_cache_path


config_name = os.getenv("MAESTRAL_CONFIG", "maestral")

# set up logging
logger = logging.getLogger(__name__)

log_file = get_log_path("maestral", config_name + ".log")
log_fmt = logging.Formatter(fmt="%(asctime)s %(name)s %(levelname)s: %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
rfh = logging.handlers.WatchedFileHandler(log_file)
rfh.setFormatter(log_fmt)
rfh.setLevel(CONF.get("app", "log_level_file"))

# set up logging to stdout
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(log_fmt)
sh.setLevel(CONF.get("app", "log_level_console"))


# set up logging to stream
class CachedHandler(logging.Handler):
    """
    Handler which remembers the last record only.
    """
    def __init__(self):
        logging.Handler.__init__(self)
        self.lastRecord = None

    def emit(self, record):
        self.lastRecord = record

    def getLastRecord(self):
        if self.lastRecord:
            return self.lastRecord.getMessage()
        else:
            return ""


ch = CachedHandler()
ch.setLevel(logging.INFO)

# add handlers
mdbx_logger = logging.getLogger("maestral")
mdbx_logger.setLevel(logging.DEBUG)
mdbx_logger.addHandler(rfh)
mdbx_logger.addHandler(sh)
mdbx_logger.addHandler(ch)


def folder_download_worker(monitor, dbx_path, callback=None):
    """
    Worker to download a whole Dropbox directory in the background.

    :param class monitor: :class:`Monitor` instance.
    :param str dbx_path: Path to directory on Dropbox.
    :param callback: function to be called after download is complete
    """
    download_complete_signal = signal("download_complete_signal")

    time.sleep(2)  # wait for pausing to take effect

    with monitor.sync.lock:
        completed = False
        while not completed:
            try:
                monitor.sync.get_remote_dropbox(dbx_path)
                logger.info(IDLE)

                if dbx_path == "":
                    monitor.sync.last_sync = time.time()
                else:
                    # remove folder from excluded list
                    monitor.queue_downloading.queue.remove(
                        monitor.sync.to_local_path(dbx_path))

                time.sleep(1)
                completed = True
                if callback is not None:
                    callback()
                download_complete_signal.send()

            except CONNECTION_ERRORS as e:
                logger.warning("{0}: {1}".format(CONNECTION_ERROR_MSG, e))
                logger.info(DISCONNECTED)


def with_sync_paused(func):
    """
    Decorator which pauses syncing before a method call, resumes afterwards. This
    should only be used to decorate Maestral methods.
    """
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        # pause syncing
        resume = False
        if self.syncing:
            self.pause_sync()
            resume = True
        ret = func(self, *args, **kwargs)
        # resume syncing if previously paused
        if resume:
            self.resume_sync()
        return ret
    return wrapper


def handle_disconnect(func):
    """
    Decorator which handles connection and auth errors during a function call and returns
    ``False`` if an error occurred.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # pause syncing
        try:
            res = func(*args, **kwargs)
            return res
        except CONNECTION_ERRORS as e:
            logger.warning("{0}: {1}".format(CONNECTION_ERROR_MSG, e))
            return False
        except DropboxAuthError as e:
            logger.exception("{0}: {1}".format(e.title, e.message))
            return False

    return wrapper


class Maestral(object):
    """
    An open source Dropbox client for macOS and Linux to syncing a local folder
    with your Dropbox account.
    """

    _daemon_running = True  # this is for running maestral as a daemon only

    def __init__(self, run=True):

        self.client = MaestralApiClient()

        # monitor needs to be created before any decorators are called
        self.monitor = MaestralMonitor(self.client)
        self.sync = self.monitor.sync

        if run:
            # if `run == False`, make sure that you manually initiate the first sync
            # before calling `start_sync`
            if self.pending_dropbox_folder():
                self.create_dropbox_directory()
                self.set_excluded_folders()

                self.sync.last_cursor = ""
                self.sync.last_sync = None

            if self.pending_first_download():
                self.get_remote_dropbox_async("", callback=self.start_sync)
            else:
                self.get_account_info()
                self.start_sync()

    @staticmethod
    def pending_link():
        """Bool indicating if auth tokens are stored in the system's keychain."""
        auth_session = OAuth2Session()
        return auth_session.load_token() is None

    @staticmethod
    def pending_dropbox_folder():
        """Bool indicating if a local Dropbox directory has been set."""
        return not osp.isdir(CONF.get("main", "path"))

    @staticmethod
    def pending_first_download():
        """Bool indicating if the initial download has already occured.."""
        return (CONF.get("internal", "lastsync") is None or
                CONF.get("internal", "cursor") == "")

    @property
    def syncing(self):
        """Bool indicating if syncing is running or paused."""
        return self.monitor.syncing.is_set()

    @property
    def connected(self):
        """Bool indicating if Dropbox servers can be reached."""
        return self.monitor.connected.is_set()

    @property
    def status(self):
        """Returns a string with the last status message. This can be displayed as
        information to the user but should not be relied on otherwise."""
        return ch.getLastRecord()

    @property
    def notify(self):
        """Bool indicating if notifications are enabled or disabled."""
        return self.sync.notify.enabled

    @notify.setter
    def notify(self, boolean):
        """Setter: Bool indicating if notifications are enabled."""
        self.sync.notify.enabled = boolean

    @property
    def dropbox_path(self):
        """Returns the path to the local Dropbox directory. Read only. Use
        :meth:`create_dropbox_directory` or :meth:`move_dropbox_directory` to set or
        change the Dropbox directory location instead. """
        return self.sync.dropbox_path

    # TODO: return a list of dictionaries or strings instead for safe serializations
    @property
    def sync_errors(self):
        """Returns list containing the current sync errors."""
        return list(self.sync.sync_errors.queue)

    @property
    def account_profile_pic_path(self):
        """Returns the path of the current account's profile picture. There may not be
        an actual file at that path, if the user did not set a profile picture or the
        picture has not yet been downloaded."""
        return get_cache_path("maestral", config_name + "_profile_pic.jpeg")

    def get_file_status(self, local_path):
        """
        Returns the sync status of an individual file.

        :param local_path: Path to file on the local drive.
        :return: String indicating the sync status. Can be "uploading", "downloading",
            "up to date", "error", or "unwatched" (for files outside of the Dropbox
            directory).
        :rtype: str
        """
        if not self.syncing:
            return "unwatched"

        try:
            dbx_path = self.sync.to_dbx_path(local_path)
        except ValueError:
            return "unwatched"

        if local_path in self.monitor.upload_list:
            return "uploading"
        elif local_path in self.monitor.download_list:
            return "downloading"
        elif local_path in self.sync_errors:
            return "error"
        elif self.sync.get_local_rev(dbx_path):
            return "up to date"
        else:
            return "unwatched"

    @handle_disconnect
    def get_account_info(self):
        """
        Gets account information stored by Dropbox and returns it as a dictionary.
        The entries will either be of type ``str`` or ``bool``.

        :returns: Dropbox account information.
        :rtype: dict
        """
        res = self.client.get_account_info()
        res_dict = dropbox_stone_to_dict(res)
        return remove_tags(res_dict)

    @handle_disconnect
    def get_profile_pic(self):
        """
        Download the user's profile picture from Dropbox. The picture saved in Maestral's
        cache directory for retrieval when there is no internet connection.

        :returns: Path to saved profile picture or None if no profile picture is set.
        """

        try:
            res = self.client.get_account_info()
        except MaestralApiError:
            pass
        else:
            if res.profile_photo_url:
                # download current profile pic
                r = requests.get(res.profile_photo_url)
                with open(self.account_profile_pic_path, "wb") as f:
                    f.write(r.content)
                return self.account_profile_pic_path
            else:
                # delete current profile pic
                self._delete_old_profile_pics()

    @staticmethod
    def _delete_old_profile_pics():
        # delete all old pictures
        for file in os.listdir(get_cache_path("maestral")):
            if file.startswith(config_name + "_profile_pic"):
                try:
                    os.unlink(osp.join(get_cache_path("maestral"), file))
                except OSError:
                    pass

    @handle_disconnect
    def get_remote_dropbox_async(self, dbx_path, callback=None):
        """
        Runs `sync.get_remote_dropbox` in the background, downloads the full
        Dropbox folder `dbx_path` to the local drive. The folder is temporarily
        excluded from the local observer to prevent duplicate uploads.

        :param str dbx_path: Path to folder on Dropbox.
        :param callback: Function to call after download.
        """

        is_root = dbx_path == ""
        if not is_root:  # exclude only specific folder otherwise
            self.monitor.queue_downloading.put(self.sync.to_local_path(dbx_path))

        self.download_thread = Thread(
                target=folder_download_worker,
                args=(self.monitor, dbx_path),
                kwargs={"callback": callback},
                name="MaestralFolderDownloader")
        self.download_thread.start()

    def rebuild_index(self):
        """Rebuilds the Maestral index and resumes syncing afterwards if it has been
        running."""

        print("""
Rebuilding the revision index. This process may
take several minutes, depending on the size of your Dropbox.
Any changes to local files during this process may be lost.""")

        self.monitor.rebuild_rev_file()

    def start_sync(self, overload=None):
        """
        Creates syncing threads and starts syncing.
        """
        self.monitor.start()
        logger.info(IDLE)

    def resume_sync(self, overload=None):
        """
        Resumes the syncing threads if paused.
        """
        self.monitor.resume()

    def pause_sync(self, overload=None):
        """
        Pauses the syncing threads if running.
        """
        self.monitor.pause()

    def stop_sync(self, overload=None):
        """
        Stops the syncing threads if running, destroys observer thread.
        """
        self.monitor.stop()

    def unlink(self):
        """
        Unlink the configured Dropbox account but leave all downloaded files
        in place. All syncing metadata will be removed as well.
        """
        self.stop_sync()
        self.client.unlink()

        try:
            os.remove(self.sync.rev_file_path)
        except OSError:
            pass

        CONF.reset_to_defaults()
        CONF.set("main", "default_dir_name", "Dropbox ({0})".format(config_name.capitalize()))

        logger.info("Unlinked Dropbox account.")

    def exclude_folder(self, dbx_path):
        """
        Excludes folder from sync and deletes local files. It is safe to call
        this method with folders which have already been excluded.

        :param str dbx_path: Dropbox folder to exclude.
        """

        dbx_path = dbx_path.lower().rstrip(osp.sep)

        # add the path to excluded list
        folders = self.sync.excluded_folders
        if dbx_path not in folders:
            folders.append(dbx_path)
        else:
            logger.info("Folder was already excluded, nothing to do.")
            return

        self.sync.excluded_folders = folders
        self.sync.set_local_rev(dbx_path, None)

        # remove folder from local drive
        local_path = self.sync.to_local_path(dbx_path)
        local_path_cased = path_exists_case_insensitive(local_path)
        logger.info("Deleting folder '{}'.".format(local_path_cased))
        if osp.isdir(local_path_cased):
            shutil.rmtree(local_path_cased)

    @handle_disconnect
    def include_folder(self, dbx_path):
        """
        Includes folder in sync and downloads in the background. It is safe to
        call this method with folders which have already been included, they
        will not be downloaded again.

        :param str dbx_path: Dropbox folder to include.
        :return: ``True`` on success, ``False`` on failure.
        :rtype: bool
        :raises: ValueError if ``dbx_path`` is inside another excluded folder.
        """

        dbx_path = dbx_path.lower().rstrip(osp.sep)

        old_excluded_folders = self.sync.excluded_folders

        for p in old_excluded_folders:
            if is_child(dbx_path, p):
                raise ValueError("'{0}' lies inside the excluded folder {1}. "
                                 "Please include {1} first.".format(dbx_path, p))

        # Get folders which will need to be downloaded, do not attempt to download
        # subfolders of `dbx_path` which were already included.
        # `new_included_folders` will either be empty (`dbx_path` was already
        # included), just contain `dbx_path` itself (the whole folder was excluded) or
        # only contain subfolders of `dbx_path` (`dbx_path` was partially included).
        new_included_folders = (x for x in old_excluded_folders if is_child(x, dbx_path))

        if new_included_folders:
            # remove `dbx_path` or all excluded children from the excluded list
            excluded_folders = list(set(old_excluded_folders) - set(new_included_folders))
        else:
            logger.info("Folder was already included, nothing to do.")
            return

        self.sync.excluded_folders = excluded_folders

        # download folder contents from Dropbox
        logger.info("Downloading added folder '{}'.".format(dbx_path))
        for folder in new_included_folders:
            self.get_remote_dropbox_async(folder)

    @handle_disconnect
    def _include_folder_without_subfolders(self, dbx_path):
        """Sets a folder to included without explicitly including its subfolders. This
        is to be used internally, when a folder has been removed from the excluded list,
        but some of its subfolders may have been added."""

        dbx_path = dbx_path.lower().rstrip(osp.sep)
        excluded_folders = self.sync.excluded_folders

        if dbx_path not in excluded_folders:
            return

        excluded_folders.remove(dbx_path)
        self.sync.excluded_folders = excluded_folders

        self.get_remote_dropbox_async(dbx_path)

    @handle_disconnect
    def set_excluded_folders(self, folder_list=None):
        """
        Sets the list of excluded folders to `folder_list`. If not given, gets all top
        level folder paths from Dropbox and asks user to include or exclude. Folders
        which are no in `folder_list` but exist on Dropbox will be downloaded.

        On initial sync, this does not trigger any downloads. Call `get_remote_dropbox` or
        `get_remote_dropbox_async` instead.

        :param list folder_list: If given, list of excluded folder to set.
        :return: List of excluded folders.
        :rtype: list
        """

        if not folder_list:

            excluded_folders = []

            # get all top-level Dropbox folders
            result = self.client.list_folder("", recursive=False)

            # paginate through top-level folders, ask to exclude
            for entry in result.entries:
                if isinstance(entry, files.FolderMetadata):
                    yes = yesno("Exclude '%s' from sync?" % entry.path_display, False)
                    if yes:
                        excluded_folders.append(entry.path_lower)
        else:
            excluded_folders = (f.lower().rstrip(osp.sep) for f in folder_list)
            excluded_folders = self.sync.clean_excluded_folder_list(excluded_folders)

        old_excluded_folders = self.sync.excluded_folders

        added_excluded_folders = set(excluded_folders) - set(old_excluded_folders)
        added_included_folders = set(old_excluded_folders) - set(excluded_folders)

        if not self.pending_first_download():
            # apply changes
            for path in added_excluded_folders:
                self.exclude_folder(path)
            for path in added_included_folders:
                self._include_folder_without_subfolders(path)

        self.sync.excluded_folders = excluded_folders

        return excluded_folders

    @staticmethod
    def excluded_status(dbx_path):
        """
        Returns 'excluded', 'partially excluded' or 'included'. This function will not
        check if the item actually exists on Dropbox.

        :param str dbx_path: Path to item on Dropbox.
        :returns: Excluded status.
        :rtype: str
        """

        dbx_path = dbx_path.lower().rstrip(osp.sep)

        excluded_items = CONF.get("main", "excluded_folders") + CONF.get("main", "excluded_files")

        if dbx_path in excluded_items:
            return "excluded"
        elif any(is_child(dbx_path, f) for f in excluded_items):
            return "partially excluded"
        else:
            return "included"

    @with_sync_paused
    def move_dropbox_directory(self, new_path=None):
        """
        Change or set local dropbox directory. This moves all local files to
        the new location. If a file or folder already exists at this location,
        it will be overwritten.

        :param str new_path: Full path to local Dropbox folder. If not given, the
            user will be prompted to input the path.
        """

        # get old and new paths
        old_path = self.sync.dropbox_path
        default_path = osp.join(get_home_dir(), CONF.get("main", "default_dir_name"))
        if new_path is None:
            new_path = self._ask_for_path(default=default_path)

        if osp.exists(old_path) and osp.exists(new_path):
            if osp.samefile(old_path, new_path):
                # nothing to do
                return

        # remove existing items at current location
        try:
            os.unlink(new_path)
        except IsADirectoryError:
            shutil.rmtree(new_path, ignore_errors=True)
        except FileNotFoundError:
            pass

        # move folder from old location or create a new one if no old folder exists
        if osp.isdir(old_path):
            shutil.move(old_path, new_path)
        else:
            os.makedirs(new_path)

        # update config file and client
        self.sync.dropbox_path = new_path

    @with_sync_paused
    def create_dropbox_directory(self, path=None, overwrite=True):
        """
        Set a new local dropbox directory.

        :param str path: Full path to local Dropbox folder. If not given, the user will be
            prompted to input the path.
        :param bool overwrite: If ``True``, any existing file or folder at ``new_path``
            will be replaced.
        """
        # ask for new path
        if path is None:
            path = self._ask_for_path()

        if overwrite:
            # remove any old items at the location
            try:
                os.unlink(path)
            except IsADirectoryError:
                shutil.rmtree(path, ignore_errors=True)
            except FileNotFoundError:
                pass

        # create new folder
        os.makedirs(path, exist_ok=True)

        # update config file and client
        self.sync.dropbox_path = path

    @staticmethod
    def _ask_for_path(default=osp.join("~", CONF.get("main", "default_dir_name"))):
        """
        Asks for Dropbox path.
        """
        while True:
            msg = ("Please give Dropbox folder location or press enter for default "
                   "[{0}]:".format(default))
            res = input(msg).strip().strip("'")

            dropbox_path = osp.expanduser(res or default)

            if osp.exists(dropbox_path):
                msg = "Directory '{0}' already exist. Do you want to overwrite it?".format(dropbox_path)
                yes = yesno(msg, True)
                if yes:
                    return dropbox_path
                else:
                    pass
            else:
                return dropbox_path

    @staticmethod
    def set_log_level_file(level):
        """Sets the log level for the file log. Changes will persist between
        restarts."""
        rfh.setLevel(level)
        CONF.set("app", "log_level_file", level)

    @staticmethod
    def set_log_level_console(level):
        """Sets the log level for the console log. Changes will persist between
        restarts."""
        sh.setLevel(level)
        CONF.set("app", "log_level_console", level)

    def shutdown_daemon(self):
        """Does nothing except for setting the _daemon_running flag ``False``. This
        will be checked by Pyro4 periodically to shut down the daemon when requested."""
        self._daemon_running = False

    def _shutdown_requested(self):
        return self._daemon_running

    def __repr__(self):
        if self.connected:
            email = CONF.get("account", "email")
            account_type = CONF.get("account", "type")
            inner = "{0}, {1}".format(email, account_type)
        else:
            inner = DISCONNECTED

        return "<{0}({1})>".format(self.__class__.__name__, inner)


def yesno(message, default):
    """Handy helper function to ask a yes/no question.

    A blank line returns the default, and answering
    y/yes or n/no returns True or False.
    Retry on unrecognized answer.
    Special answers:
    - q or quit exits the program
    - p or pdb invokes the debugger
    """
    if default:
        message += " [Y/n] "
    else:
        message += " [N/y] "
    while True:
        answer = input(message).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        if answer in ("q", "quit"):
            print("Exit")
            raise SystemExit(0)
        if answer in ("p", "pdb"):
            import pdb
            pdb.set_trace()
        print("Please answer YES or NO.")
