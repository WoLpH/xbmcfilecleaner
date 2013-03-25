# encoding: utf-8

import os
import sys
import platform
import time
import re
import ctypes
from sqlite3 import connect, OperationalError

import xbmc
import xbmcaddon
import xbmcvfs


# Addon info
__title__ = "XBMC File Cleaner"
__author__ = "Andrew Higginson <azhigginson@gmail.com>"
__addonID__ = "script.filecleaner"
__icon__ = "special://home/addons/" + __addonID__ + "/icon.png"
__settings__ = xbmcaddon.Addon(__addonID__)


class Main:
    # Constants to ensure correct SQL queries
    MOVIES = "movie"
    MUSIC_VIDEOS = "musicvideo"
    TVSHOWS = "episode"

    def __init__(self):
        """Create a Main object that performs regular cleaning of watched videos."""
        self.reload_settings()

        service_sleep = 10
        ticker = 0
        delayed_completed = False

        # TODO should be removed: http://ziade.org/2008/01/08/syssetdefaultencoding-is-evil/
        reload(sys)
        sys.setdefaultencoding("utf-8")

        while not xbmc.abortRequested:
            self.reload_settings()

            scanInterval_ticker = self.scan_interval * 60 / service_sleep
            delayedStart_ticker = self.delayed_start * 60 / service_sleep

            if not self.deleting_enabled:
                continue
                #elif  not self.runAsService:
                #continue
            else:
                if delayed_completed and ticker >= scanInterval_ticker:
                    self.cleanup()
                    ticker = 0
                elif not delayed_completed and ticker >= delayedStart_ticker:
                    delayed_completed = True
                    self.cleanup()
                    ticker = 0

                time.sleep(service_sleep)
                ticker += 1

        # Abort is requested by XBMC: terminate
        self.debug(__settings__.getLocalizedString(34007))

    def cleanup(self):
        """Delete any watched videos from the XBMC video database.
        The videos to be deleted are subject to a number of criteria as can be specified in the addon's settings.
        """
        self.debug("Starting cleaning routine")

        if self.delete_when_idle and xbmc.Player().isPlayingVideo():
            self.debug("A video is currently being played. No cleaning will be performed during this interval.")
            return

        # TODO combine these functionalities into a single loop
        if not self.delete_when_low_disk_space or (self.delete_when_low_disk_space and self.disk_space_low()):
            # create stub to summarize cleaning results
            summary = "Deleted" if not self.holding_enabled else "Moved"
            cleaning_required = False
            if self.delete_movies:
                movies = self.get_expired(self.MOVIES)
                if movies:
                    count = 0
                    for abs_path in movies:
                        abs_path = str(*abs_path)  # Convert 1 element tuple into string with scatter
                        if xbmcvfs.exists(abs_path):
                            cleaning_required = True
                            if self.holding_enabled:
                                if self.move_file(abs_path, self.holding_folder):
                                    count += 1
                                    self.delete_empty_folders(os.path.dirname(abs_path))
                            else:
                                if self.delete_file(abs_path):
                                    count += 1
                                    self.delete_empty_folders(os.path.dirname(abs_path))
                        else:
                            self.debug("XBMC could not find the file at %s" % abs_path)
                    if count > 0:
                        summary += " %d %s(s)" % (count, self.MOVIES)

            if self.delete_tv_shows:
                episodes = self.get_expired(self.TVSHOWS)
                if episodes:
                    count = 0
                    for abs_path, idFile, show_name, season_number in episodes:
                        if xbmcvfs.exists(abs_path):
                            cleaning_required = True
                            if self.holding_enabled:
                                if self.create_series_season_dirs:
                                    new_path = os.path.join(self.holding_folder, show_name, "Season " + season_number)
                                else:
                                    new_path = self.holding_folder
                                if self.move_file(abs_path, new_path):
                                    count += 1
                                    self.delete_empty_folders(os.path.dirname(abs_path))
                            else:
                                if self.delete_file(abs_path):
                                    count += 1
                                    self.delete_empty_folders(os.path.dirname(abs_path))
                        else:
                            self.debug("XBMC could not find the file at %s" % abs_path)
                    if count > 0:
                        summary += " %d %s(s)" % (count, self.TVSHOWS)

            if self.delete_music_videos:
                musicvideos = self.get_expired(self.MUSIC_VIDEOS)
                if musicvideos:
                    count = 0
                    for abs_path in musicvideos:
                        abs_path = str(*abs_path)  # Convert 1 element tuple into string with scatter
                        if xbmcvfs.exists(abs_path):
                            cleaning_required = True
                            if self.holding_enabled:
                                if self.move_file(abs_path, self.holding_folder):
                                    count += 1
                                    self.delete_empty_folders(os.path.dirname(abs_path))
                            else:
                                if self.delete_file(abs_path):
                                    count += 1
                                    self.delete_empty_folders(os.path.dirname(abs_path))
                        else:
                            self.debug("XBMC could not find the file at %s" % abs_path)
                    if count > 0:
                        summary += " %d %s(s)" % (count, self.MUSIC_VIDEOS)

            # Give a status report if any deletes occurred
            if not (summary.endswith("ed")):
                self.notify(summary)

            # Finally clean the library to account for any deleted videos.
            if self.clean_xbmc_library and cleaning_required:
                # Wait 10 seconds for deletions to finish before cleaning.
                time.sleep(10)

                pause = 5
                iterations = 0
                limit = self.scan_interval - pause
                # Check if the library is being updated before cleaning up
                while xbmc.getCondVisibility("Library.IsScanningVideo"):
                    iterations += 1

                    # Make sure we don't mess up the scan interval timing by waiting too long.
                    if iterations * pause >= limit:
                        iterations = 0
                        break

                    self.debug(
                        "The video library is currently being updated, waiting %d minutes before cleaning up." % pause)
                    time.sleep(pause * 60)

                xbmc.executebuiltin("XBMC.CleanLibrary(video)")

    def get_expired(self, option):
        """Retrieve a list of episodes that have been watched and match any criteria set in the addon's settings.

        Keyword arguments:
        option -- the type of videos to remove, can be one of the constants MOVIES, TVSHOWS or MUSIC_VIDEOS
        """
        self.debug("Looking for watched videos")

        results = []
        margin = 0.000001

        # First we shall build the query to be executed on the video databases
        query = "SELECT strPath || strFilename as FullPath"
        if option == "episode":
            query += ", idFile, strTitle as Show, c12 as Season"
        query += " FROM %sview" % option  # episodeview, movieview or musicvideoview
        query += " WHERE playCount > 0"

        if self.holding_enabled:
            query += " AND NOT strPath like '%s%%'" % self.holding_folder

        if self.enable_expiration:
            query += " AND lastPlayed < datetime('now', '-%d days', 'localtime')" % self.expire_after

        if self.delete_when_low_rated and option is not self.MUSIC_VIDEOS:
            column = "c05" if option is self.MOVIES else "c03"
            query += " AND %s BETWEEN %f AND %f" % \
                     (column, (margin if self.ignore_no_rating else 0), self.minimum_rating - margin)
            if self.minimum_rating != 10.000000:
                # somehow 10.000000 is considered to be between 0.000001 and x.999999
                query += " AND %s <> 10.000000" % column

        try:
            # After building the query we can execute it on any video databases we find
            _, files, = xbmcvfs.listdir(xbmc.translatePath("special://database/"))
            for database in files:
                if database.startswith("MyVideos") and database.endswith(".db"):
                    con = connect(xbmc.translatePath("special://database/" + database))
                    cur = con.cursor()

                    self.debug("Executing query on %s: %s" % (database, query))
                    cur.execute(query)

                    # Append the results to the list of files to delete.
                    results += cur.fetchall()

            return results
        except OSError, e:
            self.debug("Something went wrong while opening the database folder (errno: %d)" % e.errno)
            raise
        except OperationalError, oe:
            # The video database(s) could not be opened, or the query was invalid
            self.notify(__settings__.getLocalizedString(34002), 15000)
            msg = oe.args[0]
            self.debug("The following error occurred: '%s'" % msg)
        finally:
            cur.close()
            con.close()

    def reload_settings(self):
        """Retrieve new values for all settings, in order to account for any recent changes."""
        __settings__ = xbmcaddon.Addon(__addonID__)

        self.deleting_enabled = bool(__settings__.getSetting("deleting_enabled") == "true")
        self.delete_folders = bool(__settings__.getSetting("delete_folders") == "true")
        self.ignore_extensions = str(__settings__.getSetting("ignore_extensions"))
        self.delayed_start = float(__settings__.getSetting("delayed_start"))
        self.scan_interval = float(__settings__.getSetting("scan_interval"))

        self.notifications_enabled = bool(__settings__.getSetting("notifications_enabled") == "true")
        self.notify_when_idle = bool(__settings__.getSetting("notify_when_idle") == "true")
        self.debugging_enabled = bool(xbmc.translatePath(__settings__.getSetting("debugging_enabled")) == "true")

        self.clean_xbmc_library = bool(__settings__.getSetting("clean_xbmc_library") == "true")
        self.delete_movies = bool(__settings__.getSetting("delete_movies") == "true")
        self.delete_tv_shows = bool(__settings__.getSetting("delete_tv_shows") == "true")
        self.delete_music_videos = bool(__settings__.getSetting("delete_music_videos") == "true")
        self.delete_when_idle = bool(xbmc.translatePath(__settings__.getSetting("delete_when_idle")) == "true")

        self.enable_expiration = bool(__settings__.getSetting("enable_expiration") == "true")
        self.expire_after = float(__settings__.getSetting("expire_after"))

        self.delete_when_low_rated = bool(__settings__.getSetting("delete_when_low_rated") == "true")
        self.minimum_rating = float(__settings__.getSetting("minimum_rating"))
        self.ignore_no_rating = bool(__settings__.getSetting("ignore_no_rating") == "true")

        self.delete_when_low_disk_space = bool(__settings__.getSetting("delete_when_low_disk_space") == "true")
        self.disk_space_threshold = float(__settings__.getSetting("disk_space_threshold"))
        self.disk_space_check_path = xbmc.translatePath(__settings__.getSetting("disk_space_check_path"))

        self.holding_enabled = bool(__settings__.getSetting("holding_enabled") == "true")
        self.holding_folder = xbmc.translatePath(__settings__.getSetting("holding_folder"))
        self.create_series_season_dirs = bool(
            xbmc.translatePath(__settings__.getSetting("create_series_season_dirs")) == "true")

    def get_free_disk_space(self, path):
        """Determine the percentage of free disk space.

        Keyword arguments:
        path -- the path to the drive to check (this can be any path of any length on the desired drive).
        If the path doesn't exist, this function returns 100, in order to prevent files from being deleted accidentally.
        """
        # TODO: Check disk space for each file that matches the deleting criteria, and only delete if it frees up space
        percentage = float(100)
        self.debug("Checking for disk space on path: %s" % path)
        if xbmcvfs.exists(path):  # Fails for drive-only paths like "E:\"
            if platform.system() == "Windows":
                self.debug("We are checking disk space from a Windows file system")
                self.debug("The current path is %s" % path)
                self.debug("Stripping the path of all redundant stuff.")

                if r"://" in path:
                    self.debug("We are dealing with network paths.")
                    self.debug("Extracting information from share %s" % path)

                    pattern = re.compile("(?P<type>smb|nfs|afp)://(?P<user>\w+):(?P<pass>.+)@(?P<host>.+)",
                                         flags=re.I | re.U)
                    match = pattern.match(path)
                    share = match.groupdict()

                    self.debug("Retrieved the following information:")
                    self.debug("Protocol: %s" % share["type"])
                    self.debug("User: %s" % share["user"])
                    self.debug("Pass: %s" % share["pass"])
                    self.debug("Host: %s" % share["host"])

                    self.debug("Creating UNC paths so Windows understands the shares.")

                    path = path[match.end():]

                    self.debug("New path: %s" % path)

                    path = os.path.normcase(r"\\" + share["host"] + path)
                    self.debug("os.path.normcase result:\n" + path)
                else:
                    self.debug("We are dealing with local paths:\n" + path)

                if not isinstance(path, unicode):
                    self.debug("Path must be unicode for disk space checks.")
                    path = path.decode("mbcs")
                    self.debug("New path: %s" % path)

                totalNumberOfBytes = ctypes.c_ulonglong(0)
                totalNumberOfFreeBytes = ctypes.c_ulonglong(0)

                # GetDiskFreeSpaceEx explained:
                # http://msdn.microsoft.com/en-us/library/windows/desktop/aa364937(v=vs.85).aspx
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(path), ctypes.pointer(totalNumberOfBytes),
                                                           ctypes.pointer(totalNumberOfFreeBytes), None)
                free = float(totalNumberOfBytes.value)
                capacity = float(totalNumberOfFreeBytes.value)

                try:
                    percentage = float(free / capacity * float(100))
                    self.debug("Hard disk checks returned the following results:\n%s: %f\n%s: %f\n%s: %f" %
                               ("free", free, "capacity", capacity, "percentage", percentage))
                except ZeroDivisionError, e:
                    self.notify(__settings__.getLocalizedString(34011), 15000)
            else:
                self.debug("We are checking disk space from a non-Windows file system")
                self.debug("Stripping " + path + " of all redundant stuff.")
                drive = os.path.normpath(path)
                self.debug("The path now is " + drive)

                try:
                    diskstats = os.statvfs(path)
                    percentage = float(diskstats.f_bfree / diskstats.f_blocks * float(100))
                    self.debug("Hard disk checks returned the following results:\n%s: %f\n%s: %f\n%s: %f" % (
                        "free blocks", diskstats.f_bfree, "total blocks", diskstats.f_blocks, "percentage", percentage))
                except OSError, e:
                    self.notify(__settings__.getLocalizedString(34012) % self.disk_space_check_path)
                except ZeroDivisionError, zde:
                    self.notify(__settings__.getLocalizedString(34011), 15000)
        else:
            self.notify(__settings__.getLocalizedString(34013), 15000)

        return percentage

    def disk_space_low(self):
        """Check if the disk is running low on free space.
        Returns true if the free space is less than the threshold specified in the addon's settings.
        :rtype : Boolean
        """
        return self.get_free_disk_space(self.disk_space_check_path) <= self.disk_space_threshold

    def delete_file(self, location):
        """Delete a file from the file system."""
        self.debug("Deleting file at %s" % location)
        if xbmcvfs.exists(location):
            return xbmcvfs.delete(location)
        else:
            self.debug("XBMC could not find the file at %s" % location)
            return False

    def delete_empty_folders(self, folder):
        self.debug("Checking if %s is empty" % folder)

        ignored_file_types = [file_ext.strip() for file_ext in self.ignore_extensions.split(",")]

        self.debug("Ignoring file types %s" % ignored_file_types)

        subfolders, files = xbmcvfs.listdir(folder)

        self.debug("Contents of %s:\nSubfolders:\t%s\nFiles:\t%s" % (folder, subfolders, files))

        empty = True
        try:
            for f in files:
                _, ext = os.path.splitext(f)
                self.debug("File extension: " + ext)
                if ext not in ignored_file_types:
                    self.debug("Found video file %s" % f)
                    empty = False
                    break
        except OSError, oe:
            self.debug("Error deriving file extension. Errno " + str(oe.errno))
            empty = False

        # Only delete directories if we found them to be empty (containing no files or filetypes we ignored)
        if empty:
            self.debug("Directory is empty and will be removed")
            try:
                # Recursively delete any subfolders
                for f in subfolders:
                    self.debug("Deleting file at " + str(os.path.join(folder, f)))
                    self.delete_empty_folders(os.path.join(folder, f))

                # Delete any files in the current folder
                for f in files:
                    self.debug("Deleting file at " + str(os.path.join(folder, f)))
                    xbmcvfs.delete(os.path.join(folder, f))

                # Finally delete the current folder
                xbmcvfs.rmdir(folder)
            except OSError, oe:
                self.debug("An exception occurred while deleting folders. Errno " + str(oe.errno))
        else:
            self.debug("Directory is not empty and will not be removed")

    def move_file(self, source, dest_folder):
        """Move a file to a new destination. Returns True if the move succeeded, False otherwise.
        Will create destination if it does not exist.

        Keyword arguments:
        source -- the source path (absolute)
        destination -- the destination path (absolute)
        """
        dest_folder = xbmc.makeLegalFilename(dest_folder)
        self.debug("Moving %s to %s" % (os.path.basename(source), dest_folder))
        if xbmcvfs.exists(source):
            if not xbmcvfs.exists(dest_folder):
                self.debug("XBMC could not find destination %s" % dest_folder)
                self.debug("Creating destination %s" % dest_folder)
                if xbmcvfs.mkdirs(dest_folder):
                    self.debug("Successfully created %s" % dest_folder)
                else:
                    self.debug("XBMC could not create destination %s" % dest_folder)
                    return False

            new_path = os.path.join(dest_folder, os.path.basename(source))

            if xbmcvfs.exists(new_path):
                self.debug("A file with the same name already exists in the holding folder. Checking file sizes.")
                existing_file = xbmcvfs.File(new_path)
                file_to_move = xbmcvfs.File(source)
                if file_to_move.size() > existing_file.size():
                    self.debug("This file is larger than the existing file. Replacing the existing file with this one.")
                    existing_file.close()
                    file_to_move.close()
                    return xbmcvfs.delete(new_path) and xbmcvfs.rename(source, new_path)
                else:
                    self.debug("This file is smaller than the existing file. Deleting this file instead of moving.")
                    existing_file.close()
                    file_to_move.close()
                    return self.delete_file(source)
            else:
                self.debug("Moving %s\nto %s\nNew path: %s" % (source, dest_folder, new_path))
                return xbmcvfs.rename(source, new_path)
        else:
            self.debug("XBMC could not find the file at %s" % source)
            return False

    def notify(self, message, duration=5000, image=__icon__):
        """Display an XBMC notification and log the message.

        Keyword arguments:
        message -- the message to be displayed and logged
        duration -- the duration the notification is displayed in milliseconds (default 5000)
        image -- the path to the image to be displayed on the notification (default "icon.png")
        """
        self.debug(message)
        if self.notifications_enabled:
            if self.notify_when_idle and xbmc.Player().isPlayingVideo():
                return
            xbmc.executebuiltin("XBMC.Notification(%s, %s, %s, %s)" % (__title__, message, duration, image))

    def debug(self, message):
        """logs a debug message"""
        if self.debugging_enabled:
            for line in message.splitlines():
                xbmc.log(__title__ + ": " + line)


run = Main()
