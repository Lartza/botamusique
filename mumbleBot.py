#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import threading
import time
import sys
import math
import re
import signal
import configparser
import audioop
import subprocess as sp
import argparse
import os
import os.path
import pymumble.pymumble_py3 as pymumble
import variables as var
import hashlib
import youtube_dl
import logging
import logging.handlers
import traceback
from packaging import version

import util
import command
import constants
from database import Database
import media.url
import media.file
import media.playlist
import media.radio
import media.system
from librb import radiobrowser
from playlist import PlayList


class MumbleBot:
    version = '5.2'

    def __init__(self, args):
        self.log = logging.getLogger("bot")
        self.log.info("bot: botamusique version %s, starting..." % self.version)
        signal.signal(signal.SIGINT, self.ctrl_caught)
        self.cmd_handle = {}
        self.volume_set = var.config.getfloat('bot', 'volume')
        if var.db.has_option('bot', 'volume'):
            self.volume_set = var.db.getfloat('bot', 'volume')

        self.volume = self.volume_set

        if args.channel:
            self.channel = args.channel
        else:
            self.channel = var.config.get("server", "channel", fallback=None)

        if args.verbose:
            self.log.setLevel(logging.DEBUG)
            self.log.debug("Starting in DEBUG loglevel")
        elif args.quiet:
            self.log.setLevel(logging.ERROR)
            self.log.error("Starting in ERROR loglevel")

        var.user = args.user
        var.music_folder = util.solve_filepath(var.config.get('bot', 'music_folder'))
        var.tmp_folder = util.solve_filepath(var.config.get('bot', 'tmp_folder'))
        var.is_proxified = var.config.getboolean(
            "webinterface", "is_web_proxified")
        self.exit = False
        self.nb_exit = 0
        self.thread = None
        self.thread_stderr = None
        self.is_pause = False
        self.playhead = -1
        self.song_start_at = -1
        #self.download_threads = []
        self.wait_for_downloading = False # flag for the loop are waiting for download to complete in the other thread

        if var.config.getboolean("webinterface", "enabled"):
            wi_addr = var.config.get("webinterface", "listening_addr")
            wi_port = var.config.getint("webinterface", "listening_port")
            tt = threading.Thread(
                target=start_web_interface, name="WebThread", args=(wi_addr, wi_port))
            tt.daemon = True
            self.log.info('Starting web interface on {}:{}'.format(wi_addr, wi_port))
            tt.start()

        if var.config.getboolean("bot", "auto_check_update"):
            th = threading.Thread(target=self.check_update, name="UpdateThread")
            th.daemon = True
            th.start()

        if args.host:
            host = args.host
        else:
            host = var.config.get("server", "host")

        if args.port:
            port = args.port
        else:
            port = var.config.getint("server", "port")

        if args.password:
            password = args.password
        else:
            password = var.config.get("server", "password")

        if args.channel:
            self.channel = args.channel
        else:
            self.channel = var.config.get("server", "channel")

        if args.certificate:
            certificate = args.certificate
        else:
            certificate = util.solve_filepath(var.config.get("server", "certificate"))

        if args.tokens:
            tokens = args.tokens
        else:
            tokens = var.config.get("server", "tokens")
            tokens = tokens.split(',')

        if args.user:
            self.username = args.user
        else:
            self.username = var.config.get("bot", "username")

        self.mumble = pymumble.Mumble(host, user=self.username, port=port, password=password, tokens=tokens,
                                      debug=var.config.getboolean('debug', 'mumbleConnection'), certfile=certificate)
        self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self.message_received)

        self.mumble.set_codec_profile("audio")
        self.mumble.start()  # start the mumble thread
        self.mumble.is_ready()  # wait for the connection
        self.set_comment()
        self.mumble.users.myself.unmute()  # by sure the user is not muted
        if self.channel:
            self.mumble.channels.find_by_name(self.channel).move_in()
        self.mumble.set_bandwidth(200000)

        self.is_ducking = False
        self.on_ducking = False
        self.ducking_release = time.time()

        if not var.db.has_option("bot", "ducking") and var.config.getboolean("bot", "ducking", fallback=False)\
                or var.config.getboolean("bot", "ducking"):
            self.is_ducking = True
            self.ducking_volume = var.config.getfloat("bot", "ducking_volume", fallback=0.05)
            self.ducking_volume = var.db.getfloat("bot", "ducking_volume", fallback=self.ducking_volume)
            self.ducking_threshold = var.config.getfloat("bot", "ducking_threshold", fallback=5000)
            self.ducking_threshold = var.db.getfloat("bot", "ducking_threshold", fallback=self.ducking_threshold)
            self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_SOUNDRECEIVED, \
                                               self.ducking_sound_received)
            self.mumble.set_receive_sound(True)

        # Debug use
        self._loop_status = 'Idle'
        self._display_rms = False
        self._max_rms = 0

    # Set the CTRL+C shortcut
    def ctrl_caught(self, signal, frame):

        self.log.info(
            "\nSIGINT caught, quitting, {} more to kill".format(2 - self.nb_exit))
        self.exit = True
        self.pause()
        if self.nb_exit > 1:
            self.log.info("Forced Quit")
            sys.exit(0)
        self.nb_exit += 1

    def check_update(self):
        self.log.debug("update: checking for updates...")
        new_version = util.new_release_version()
        if version.parse(new_version) > version.parse(self.version):
            self.log.info("update: new version %s found, current installed version %s." % (new_version, self.version))
            self.send_msg(constants.strings('new_version_found'))
        else:
            self.log.debug("update: no new version found.")

    def register_command(self, cmd, handle):
        cmds = cmd.split(",")
        for command in cmds:
            command = command.strip()
            if command:
                self.cmd_handle[command] = handle
                self.log.debug("bot: command added: " + command)

    def set_comment(self):
        self.mumble.users.myself.comment(var.config.get('bot', 'comment'))

    # =======================
    #         Message
    # =======================

    # All text send to the chat is analysed by this function
    def message_received(self, text):
        message = text.message.strip()
        user = self.mumble.users[text.actor]['name']

        if var.config.getboolean('commands', 'split_username_at_space'):
            # in can you use https://github.com/Natenom/mumblemoderator-module-collection/tree/master/os-suffixes ,
            # you want to split the username
            user = user.split()[0]

        if message[0] in var.config.get('commands', 'command_symbol'):
            # remove the symbol from the message
            message = message[1:].split(' ', 1)

            # use the first word as a command, the others one as  parameters
            if len(message) > 0:
                command = message[0]
                parameter = ''
                if len(message) > 1:
                    parameter = message[1].rstrip()
            else:
                return

            self.log.info('bot: received command ' + command + ' - ' + parameter + ' by ' + user)

            # Anti stupid guy function
            if not self.is_admin(user) and not var.config.getboolean('bot', 'allow_other_channel_message') and self.mumble.users[text.actor]['channel_id'] != self.mumble.users.myself['channel_id']:
                self.mumble.users[text.actor].send_text_message(
                    constants.strings('not_in_my_channel'))
                return

            if not self.is_admin(user) and not var.config.getboolean('bot', 'allow_private_message') and text.session:
                self.mumble.users[text.actor].send_text_message(
                    constants.strings('pm_not_allowed'))
                return

            for i in var.db.items("user_ban"):
                if user.lower() == i[0]:
                    self.mumble.users[text.actor].send_text_message(
                        constants.strings('user_ban'))
                    return

            if parameter:
                for i in var.db.items("url_ban"):
                    if util.get_url_from_input(parameter.lower()) == i[0]:
                        self.mumble.users[text.actor].send_text_message(
                            constants.strings('url_ban'))
                        return


            command_exc = ""
            try:
                if command in self.cmd_handle:
                    command_exc = command
                    self.cmd_handle[command](self, user, text, command, parameter)
                else:
                    # try partial match
                    cmds = self.cmd_handle.keys()
                    matches = []
                    for cmd in cmds:
                        if cmd.startswith(command):
                            matches.append(cmd)

                    if len(matches) == 1:
                        self.log.info("bot: {:s} matches {:s}".format(command, matches[0]))
                        command_exc = matches[0]
                        self.cmd_handle[command_exc](self, user, text, command_exc, parameter)
                    elif len(matches) > 1:
                        self.mumble.users[text.actor].send_text_message(
                            constants.strings('which_command', commands="<br>".join(matches)))
                    else:
                        self.mumble.users[text.actor].send_text_message(
                            constants.strings('bad_command', command=command))
            except:
                error_traceback = traceback.format_exc()
                error = error_traceback.rstrip().split("\n")[-1]
                self.log.error("bot: command %s failed with error: %s\n" % (command_exc, error_traceback))
                self.send_msg(constants.strings('error_executing_command', command=command_exc, error=error), text)

    def send_msg(self, msg, text=None):
        msg = msg.encode('utf-8', 'ignore').decode('utf-8')
        # text if the object message, contain information if direct message or channel message
        if not text or not text.session:
            own_channel = self.mumble.channels[self.mumble.users.myself['channel_id']]
            own_channel.send_text_message(msg)
        else:
            self.mumble.users[text.actor].send_text_message(msg)

    def is_admin(self, user):
        list_admin = var.config.get('bot', 'admin').rstrip().split(';')
        if user in list_admin:
            return True
        else:
            return False

    # =======================
    #   Launch and Download
    # =======================

    def launch_music(self, index=-1):
        uri = ""
        music = None
        if var.playlist.is_empty():
            return

        if index == -1:
            music = var.playlist.current_item()
        else:
            music = var.playlist.jump(index)

        self.wait_for_downloading = False

        self.log.info("bot: play music " + util.format_debug_song_string(music))
        if music["type"] == "url":
            # Delete older music is the tmp folder is too big
            media.system.clear_tmp_folder(var.tmp_folder, var.config.getint('bot', 'tmp_folder_max_size'))

            if music['ready'] == 'downloading':
                self.wait_for_downloading = True
                self.log.info("bot: current music isn't ready, other thread is downloading.")
                return

            # Check if the music is ready to be played
            if music["ready"] != "yes" or not os.path.exists(music['path']):
                self.wait_for_downloading = True
                self.log.info("bot: current music isn't ready, start downloading.")
                self.async_download(index)
                return

            if music['ready'] == 'failed':
                self.log.info("bot: removing music from the playlist: %s" % util.format_debug_song_string(music))
                var.playlist.remove(index)
                return
            uri = music['path']

        elif music["type"] == "file":
            if not self.check_item_path_or_remove():
                return
            uri = var.music_folder + var.playlist.current_item()["path"]

        elif music["type"] == "radio":
            uri = music["url"]
            if 'name' not in music:
                self.log.info("bot: fetching radio server description")
                title = media.radio.get_radio_server_description(uri)
                music["name"] = title

        if var.config.getboolean('bot', 'announce_current_music'):
            self.send_msg(util.format_current_playing())

        if var.config.getboolean('debug', 'ffmpeg'):
            ffmpeg_debug = "debug"
        else:
            ffmpeg_debug = "warning"

        command = ("ffmpeg", '-v', ffmpeg_debug, '-nostdin', '-i',
                   uri, '-ac', '1', '-f', 's16le', '-ar', '48000', '-')
        self.log.debug("bot: execute ffmpeg command: " + " ".join(command))

        # The ffmpeg process is a thread
        # prepare pipe for catching stderr of ffmpeg
        pipe_rd, pipe_wd = os.pipe()
        util.pipe_no_wait(pipe_rd) # Let the pipe work in non-blocking mode
        self.thread_stderr = os.fdopen(pipe_rd)
        self.thread = sp.Popen(command, stdout=sp.PIPE, stderr=pipe_wd, bufsize=480)
        self.is_pause = False
        self.song_start_at = -1
        self.playhead = 0
        self.last_volume_cycle_time = time.time()

    def validate_music(self, music):
        url = music['url']

        url_hash = hashlib.md5(url.encode()).hexdigest()

        path = var.tmp_folder + url_hash + ".%(ext)s"
        mp3 = path.replace(".%(ext)s", ".mp3")
        music['path'] = mp3

        # Download only if music is not existed
        if os.path.isfile(mp3):
            self.log.info("bot: file existed for url %s " % music['url'])
            music['ready'] = 'yes'
            return music

        music = media.url.get_url_info(music)

        self.log.info("bot: verifying the duration of url %s " % music['url'])

        if music:
            if music['duration'] > var.config.getint('bot', 'max_track_duration'):
                # Check the length, useful in case of playlist, it wasn't checked before)
                self.log.info(
                    "the music " + music["url"] + " has a duration of " + str(music['duration']) + "s -- too long")
                self.send_msg(constants.strings('too_long'))
                return False
            else:
                music['ready'] = "no"

            return music
        else:
            self.log.error("bot: error while fetching info from the URL")
            self.send_msg(constants.strings('unable_download'))
            return False

    def download_music(self, index=-1):
        if index == -1:
            index = var.playlist.current_index
        music = var.playlist[index]

        if music['type'] != 'url':
            # then no need to download
            return music

        self.download_in_progress = True

        url = music['url']

        url_hash = hashlib.md5(url.encode()).hexdigest()

        path = var.tmp_folder + url_hash + ".%(ext)s"
        mp3 = path.replace(".%(ext)s", ".mp3")
        music['path'] = mp3

        # Download only if music is not existed
        if not os.path.isfile(mp3):
            # download the music
            music['ready'] = "downloading"
            var.playlist.update(music, music['id'])

            self.log.info("bot: downloading url (%s) %s " % (music['title'], url))
            ydl_opts = ""

            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': path,
                'noplaylist': True,
                'writethumbnail': True,
                'updatetime': False,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192'},
                    {'key': 'FFmpegMetadata'}]
            }
            self.send_msg(constants.strings('download_in_progress', item=music['title']))

            with youtube_dl.YoutubeDL(ydl_opts) as ydl:
                attempts = var.config.getint('bot', 'download_attempts', fallback=2)
                download_succeed = False
                for i in range(attempts):
                    self.log.info("bot: download attempts %d / %d" % (i+1, attempts))
                    try:
                        ydl.extract_info(url)
                        download_succeed = True
                        break
                    except:
                        error_traceback = traceback.format_exc().split("During")[0]
                        error = error_traceback.rstrip().split("\n")[-1]
                        self.log.error("bot: download failed with error:\n %s" % error)

                if download_succeed:
                    music['ready'] = "yes"
                    self.log.info(
                        "bot: finished downloading url (%s) %s, saved to %s." % (music['title'], url, music['path']))
                else:
                    for f in [mp3, path.replace(".%(ext)s", ".jpg"), path.replace(".%(ext)s", ".m4a")]:
                        if os.path.exists(f):
                            os.remove(f)
                    self.send_msg(constants.strings('unable_download'))
                    music['ready'] = "failed"
        else:
            self.log.info("bot: music file existed, skip downloading " + mp3)
            music['ready'] = "yes"

        music = util.attach_music_tag_info(music)

        var.playlist.update(music, music['id'])
        self.download_in_progress = False
        return music

    def async_download_next(self):
        # Function start if the next music isn't ready
        # Do nothing in case the next music is already downloaded
        self.log.debug("bot: Async download next asked ")
        if var.playlist.next_item() and var.playlist.next_item()['type'] == 'url':
            # usually, all validation will be done when adding to the list.
            # however, for performance consideration, youtube playlist won't be validate when added.
            # the validation has to be done here.
            while var.playlist.next_item() and var.playlist.next_item()['ready'] == "validation":
                music = self.validate_music(var.playlist.next_item())
                if music:
                    var.playlist.update(music, music['id'])
                    break
                else:
                    var.playlist.remove(var.playlist.next_index())

            if var.playlist.next_item() and var.playlist.next_item()['ready'] == "no":
                self.async_download(var.playlist.next_index())

    def async_download(self, index):
        th = threading.Thread(
            target=self.download_music, name="DownloadThread-" + var.playlist[index]['id'][:5], args=(index,))
        self.log.info(
            "bot: start downloading item in thread: " + util.format_debug_song_string(var.playlist[index]))
        th.daemon = True
        th.start()
        #self.download_threads.append(th)
        return th

    def check_item_path_or_remove(self, index = -1):
        if index == -1:
            index = var.playlist.current_index
        music = var.playlist[index]

        if music['type'] == 'radio':
            return True

        if not 'path' in music:
            return False
        else:
            if music["type"] == "url":
                uri = music['path']
                if not os.path.exists(uri):
                    music['ready'] = 'validation'
                    return False

            elif music["type"] == "file":
                uri = var.music_folder + music["path"]
                if not os.path.exists(uri):
                    self.log.info("bot: music file missed. removing music from the playlist: %s" % util.format_debug_song_string(music))
                    self.send_msg(constants.strings('file_missed', file=music["path"]))
                    var.playlist.remove(index)
                    return False

        return True

    # =======================
    #          Loop
    # =======================

    # Main loop of the Bot
    def loop(self):
        raw_music = ""
        while not self.exit and self.mumble.is_alive():

            while self.thread and self.mumble.sound_output.get_buffer_size() > 0.5 and not self.exit:
                # If the buffer isn't empty, I cannot send new music part, so I wait
                self._loop_status = 'Wait for buffer %.3f' % self.mumble.sound_output.get_buffer_size()
                time.sleep(0.01)

            if self.thread:
                # I get raw from ffmpeg thread
                # move playhead forward
                self._loop_status = 'Reading raw'
                if self.song_start_at == -1:
                    self.song_start_at = time.time() - self.playhead
                self.playhead = time.time() - self.song_start_at

                raw_music = self.thread.stdout.read(480)

                try:
                    stderr_msg = self.thread_stderr.readline()
                    if stderr_msg:
                        self.log.debug("ffmpeg: " + stderr_msg.strip("\n"))
                except:
                    pass

                if raw_music:
                    # Adjust the volume and send it to mumble
                    self.volume_cycle()
                    self.mumble.sound_output.add_sound(
                        audioop.mul(raw_music, 2, self.volume))
                else:
                    time.sleep(0.1)
            else:
                time.sleep(0.1)

            if not self.is_pause and (self.thread is None or not raw_music):
                # ffmpeg thread has gone. indicate that last song has finished. move to the next song.
                if not self.wait_for_downloading:
                    if var.playlist.next():
                    # if downloading in the other thread
                        self.launch_music()
                        self.async_download_next()
                    else:
                        self._loop_status = 'Empty queue'
                else:
                    if var.playlist.current_item():
                        if var.playlist.current_item()["ready"] != "downloading":
                            self.wait_for_downloading = False
                            self.launch_music()
                            self.async_download_next()
                        else:
                            self._loop_status = 'Wait for downloading'
                    else:
                        self.wait_for_downloading = False

        while self.mumble.sound_output.get_buffer_size() > 0:
            # Empty the buffer before exit
            time.sleep(0.01)
        time.sleep(0.5)

        if self.exit:
            self._loop_status = "exited"
            if var.config.getboolean('bot', 'save_playlist', fallback=True):
                self.log.info("bot: save playlist into database")
                var.playlist.save()

    def volume_cycle(self):
        delta = time.time() - self.last_volume_cycle_time

        if self.on_ducking and self.ducking_release < time.time():
            self._clear_pymumble_soundqueue()
            self.on_ducking = False
            self._max_rms = 0

        if delta > 0.001:
            if self.is_ducking and self.on_ducking:
                self.volume = (self.volume - self.ducking_volume) * math.exp(- delta / 0.2) + self.ducking_volume
            else:
                self.volume = self.volume_set - (self.volume_set - self.volume) * math.exp(- delta / 0.5)

        self.last_volume_cycle_time = time.time()

    def ducking_sound_received(self, user, sound):
        rms = audioop.rms(sound.pcm, 2)
        self._max_rms = max(rms, self._max_rms)
        if self._display_rms:
            if rms < self.ducking_threshold:
                print('%6d/%6d  ' % (rms, self._max_rms) + '-'*int(rms/200), end='\r')
            else:
                print('%6d/%6d  ' % (rms, self._max_rms) + '-'*int(self.ducking_threshold/200) \
                      + '+'*int((rms - self.ducking_threshold)/200), end='\r')

        if rms > self.ducking_threshold:
            if self.on_ducking is False:
                self.log.debug("bot: ducking triggered")
                self.on_ducking = True
            self.ducking_release = time.time() + 1 # ducking release after 1s

    # =======================
    #      Play Control
    # =======================

    def clear(self):
        # Kill the ffmpeg thread and empty the playlist
        if self.thread:
            self.thread.kill()
            self.thread = None
        var.playlist.clear()
        self.log.info("bot: music stopped. playlist trashed.")

    def stop(self):
        # stop and move to the next item in the playlist
        self.is_pause = True
        self.interrupt_playing()
        self.playhead = 0
        var.playlist.next()
        self.log.info("bot: music stopped.")

    def interrupt_playing(self):
        # Kill the ffmpeg thread
        if self.thread:
            self.thread.kill()
            self.thread = None
        self.song_start_at = -1

    def pause(self):
        # Kill the ffmpeg thread
        if self.thread:
            self.thread.kill()
            self.thread = None
        self.is_pause = True
        self.song_start_at = -1
        self.log.info("bot: music paused at %.2f seconds." % self.playhead)

    def resume(self):
        self.is_pause = False

        if var.playlist.current_index == -1:
            var.playlist.next()

        music = var.playlist.current_item()

        if music['type'] == 'radio' or self.playhead == 0 or not self.check_item_path_or_remove():
            self.launch_music()
            return

        if var.config.getboolean('debug', 'ffmpeg'):
            ffmpeg_debug = "debug"
        else:
            ffmpeg_debug = "warning"

        self.log.info("bot: resume music at %.2f seconds" % self.playhead)

        uri = ""
        if music["type"] == "url":
            uri = music['path']

        elif music["type"] == "file":
            uri = var.music_folder + var.playlist.current_item()["path"]

        command = ("ffmpeg", '-v', ffmpeg_debug, '-nostdin', '-ss', "%f" % self.playhead, '-i',
                   uri, '-ac', '1', '-f', 's16le', '-ar', '48000', '-')


        if var.config.getboolean('bot', 'announce_current_music'):
            self.send_msg(util.format_current_playing())

        self.log.info("bot: execute ffmpeg command: " + " ".join(command))
        # The ffmpeg process is a thread
        # prepare pipe for catching stderr of ffmpeg
        pipe_rd, pipe_wd = os.pipe()
        util.pipe_no_wait(pipe_rd) # Let the pipe work in non-blocking mode
        self.thread_stderr = os.fdopen(pipe_rd)
        self.thread = sp.Popen(command, stdout=sp.PIPE, stderr=pipe_wd, bufsize=480)
        self.last_volume_cycle_time = time.time()


    # TODO: this is a temporary workaround for issue #44 of pymumble.
    def _clear_pymumble_soundqueue(self):
        for id, user in self.mumble.users.items():
            user.sound.lock.acquire()
            user.sound.queue.clear()
            user.sound.lock.release()
        self.log.debug("bot: pymumble soundqueue cleared.")



def start_web_interface(addr, port):
    global formatter
    import interface

    # setup logger
    werkzeug_logger = logging.getLogger('werkzeug')
    logfile = util.solve_filepath(var.config.get('webinterface', 'web_logfile'))
    handler = None
    if logfile:
        handler = logging.handlers.RotatingFileHandler(logfile, mode='a', maxBytes=10240) # Rotate after 10KB
    else:
        handler = logging.StreamHandler()

    werkzeug_logger.addHandler(handler)

    interface.init_proxy()
    interface.web.env = 'development'
    interface.web.run(port=port, host=addr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Bot for playing music on Mumble')

    # General arguments
    parser.add_argument("--config", dest='config', type=str, default='configuration.ini',
                        help='Load configuration from this file. Default: configuration.ini')
    parser.add_argument("--db", dest='db', type=str,
                        default=None, help='database file. Default: database.db')

    parser.add_argument("-q", "--quiet", dest="quiet",
                        action="store_true", help="Only Error logs")
    parser.add_argument("-v", "--verbose", dest="verbose",
                        action="store_true", help="Show debug log")

    # Mumble arguments
    parser.add_argument("-s", "--server", dest="host",
                        type=str, help="Hostname of the Mumble server")
    parser.add_argument("-u", "--user", dest="user",
                        type=str, help="Username for the bot")
    parser.add_argument("-P", "--password", dest="password",
                        type=str, help="Server password, if required")
    parser.add_argument("-T", "--tokens", dest="tokens",
                        type=str, help="Server tokens, if required")
    parser.add_argument("-p", "--port", dest="port",
                        type=int, help="Port for the Mumble server")
    parser.add_argument("-c", "--channel", dest="channel",
                        type=str, help="Default channel for the bot")
    parser.add_argument("-C", "--cert", dest="certificate",
                        type=str, default=None, help="Certificate file")

    args = parser.parse_args()

    config = configparser.ConfigParser(interpolation=None, allow_no_value=True)
    parsed_configs = config.read([util.solve_filepath('configuration.default.ini'), util.solve_filepath(args.config)],
                                 encoding='utf-8')
    var.dbfile = args.db if args.db is not None else util.solve_filepath(
        config.get("bot", "database_path", fallback="database.db"))

    if len(parsed_configs) == 0:
        logging.error('Could not read configuration from file \"{}\"'.format(args.config))
        sys.exit()

    var.config = config
    var.db = Database(var.dbfile)

    # Setup logger
    bot_logger = logging.getLogger("bot")
    formatter = logging.Formatter('[%(asctime)s %(levelname)s %(threadName)s] %(message)s', "%b %d %H:%M:%S")
    bot_logger.setLevel(logging.INFO)

    logfile = util.solve_filepath(var.config.get('bot', 'logfile'))
    handler = None
    if logfile:
        handler = logging.handlers.RotatingFileHandler(logfile, mode='a', maxBytes=10240) # Rotate after 10KB
    else:
        handler = logging.StreamHandler()

    handler.setFormatter(formatter)
    bot_logger.addHandler(handler)
    var.bot_logger = bot_logger

    var.playlist = PlayList() # playlist should be initialized after the database
    var.botamusique = MumbleBot(args)
    command.register_all_commands(var.botamusique)

    # load playlist
    if var.config.getboolean('bot', 'save_playlist', fallback=True):
        var.bot_logger.info("bot: load playlist from previous session")
        var.playlist.load()

    # load playback mode
    playback_mode = None
    if var.db.has_option("playlist", "playback_mode"):
        playback_mode = var.db.get('playlist', 'playback_mode')
    else:
        playback_mode = var.config.get('bot', 'playback_mode', fallback="one-shot")

    if playback_mode in ["one-shot", "repeat", "random"]:
        var.playlist.set_mode(playback_mode)

    # Start the main loop.
    var.botamusique.loop()
