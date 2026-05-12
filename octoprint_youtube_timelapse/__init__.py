# coding=utf-8
from __future__ import absolute_import

import octoprint.plugin
from octoprint.events import Events
from octoprint.server import user_permission
from oauth2client.file import Storage
from oauth2client.client import flow_from_clientsecrets
from apiclient.discovery import build
from apiclient.http import MediaFileUpload

import os
import json
import flask
import httplib2

# This OAuth 2.0 access scope allows an application to upload files to the
# authenticated user's YouTube channel, but doesn't allow other types of access
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_API_SERVICE_NAME = "youtube"
YOUTUBE_API_VERSION = "v3"


class YoutubeTimelapsePlugin(octoprint.plugin.StartupPlugin,
                             octoprint.plugin.TemplatePlugin,
                             octoprint.plugin.SettingsPlugin,
                             octoprint.plugin.EventHandlerPlugin,
                             octoprint.plugin.SimpleApiPlugin,
                             octoprint.plugin.AssetPlugin):

    def __init__(self):
        # fas
        self.upload_events = {}

    def _add_upload_event(self, event_name, payload_path_key):
        # make sure the event exists
        if hasattr(Events, event_name):
            event = getattr(Events, event_name)
            if event not in self.upload_events:
                self.upload_events[event] = payload_path_key
            else:
                self._logger.warning('Attempted to add a duplicate movie event: %s', event_name)
        else:
            self._logger.warning('Attempted to add an event that does not exist: %s', event_name)

    def _add_all_upload_events(self):
        # clear the events set
        self.upload_events = {}
        # add the stock timelapse event
        self.upload_events[Events.MOVIE_DONE] = 'movie'
        # add any additional movie events that are stored within the settings
        for additional_event in self.additional_upload_events:
            self._add_upload_event(additional_event['event_name'], additional_event['payload_path_key'])

    def on_after_startup(self):
        # now we can add all of the movie events since the settings are loaded.
        self._add_all_upload_events()

    def on_settings_save(self, data):
        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)
        # the settings have changed, reload the movie events
        self._add_all_upload_events()

    def get_settings_defaults(self):
        return dict(
            delete_after_upload=False,
            tags="",
            privacy_status="private",
            videos_folder="/home/pi/.octoprint/timelapse",
            additional_upload_events=[
                {
                    'event_name': 'PLUGIN_OCTOLAPSE_MOVIE_DONE',
                    'payload_path_key': 'movie'
                },
                {
                    'event_name': 'PLUGIN_OCTOLAPSE_SNAPSHOT_ARCHIVE_DONE',
                    'payload_path_key': 'archive'
                }
            ],
            cert_saved=False,
            cert_authorized=False,
            installed_version=self._plugin_version
        )

    def get_template_configs(self):
        return [
            dict(type='settings', custom_bindings=True, template='youtube_timelapse_settings.jinja2')
        ]

    def is_template_autoescaped(self):
        return True

    def get_update_information(self):
        return dict(
            youtube_timelapse=dict(
                displayName="Youtube Timelapse",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="ryanfox1985",
                repo="OctoPrint-Youtube-Timelapse",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/ryanfox1985/OctoPrint-Youtube-Timelapse/archive/{target_version}.zip"
            )
        )

    @property
    def delete_after_upload(self):
        return self._settings.get_boolean(['delete_after_upload'])

    @property
    def tags(self):
        return self._settings.get(['tags'])

    @property
    def privacy_status(self):
        return self._settings.get(['privacy_status'])

    @property
    def additional_upload_events(self):
        return self._settings.get(['additional_upload_events'])

    # ~~ SimpleApiPlugin mixin

    def is_api_protected(self):
        return True

    def get_api_commands(self):
        return dict(gen_secret=["json_data"], authorize=["auth_code"], upload_videos=["videos_folder"])

    def on_api_command(self, command, data):
        if not user_permission.can():
            return flask.make_response("Insufficient rights", 403)

        client_secrets = "{}/client_secrets.json".format(self.get_plugin_data_folder())
        credentials_file = "{}/credentials.json".format(self.get_plugin_data_folder())

        if command == "gen_secret":
            # write out our client_secrets.json file
            with open(client_secrets, "w") as f:
                f.write(json.dumps(data["json_data"]))
            self._settings.set(["cert_saved"], True)
            self._settings.save()

            flow = flow_from_clientsecrets(client_secrets, scope=YOUTUBE_UPLOAD_SCOPE, redirect_uri='urn:ietf:wg:oauth:2.0:oob')
            auth_url = flow.step1_get_authorize_url()

            return flask.jsonify(dict(cert_saved=True, url=auth_url))

        elif command == "authorize":
            self._logger.info("Attempting to authorize Google App")

            flow = flow_from_clientsecrets(client_secrets, scope=YOUTUBE_UPLOAD_SCOPE, redirect_uri='urn:ietf:wg:oauth:2.0:oob')
            credentials = flow.step2_exchange(data["auth_code"])

            storage = Storage(credentials_file)
            storage.put(credentials)

            self._settings.set(["cert_authorized"], True)
            self._settings.save()
            return flask.jsonify(dict(authorized=True))

        elif command == 'upload_videos':
            for entry in os.scandir(data["videos_folder"]):
                if entry.path.endswith(".mp4"):
                    self.upload_timelapse(entry.path, data['upload_videos_delete_after'])
            return flask.make_response("Videos uploaded!", 200)

        # ~~ AssetPlugin mixin

    def on_event(self, event, payload):
        if event in self.upload_events:
            payload_path_key = self.upload_events[event]
            if payload_path_key in payload:
                file_path = payload[payload_path_key]
                file_name = os.path.basename(file_path)
                self._plugin_manager.send_plugin_message(
                    self._identifier, {'type': 'upload-start', 'file_name': file_name}
                )
                if self.upload_timelapse(file_path, self.delete_after_upload):
                    self._plugin_manager.send_plugin_message(
                        self._identifier, {'type': 'upload-success', 'file_name': file_name}
                    )
                else:
                    self._plugin_manager.send_plugin_message(
                        self._identifier, {'type': 'upload-failed', 'file_name': file_name}
                    )
            else:
                self._plugin_manager.send_plugin_message(
                    self._identifier, {'type': 'upload-failed', 'file_name': "UNKNOWN"}
                )
                self._logger.error(
                    "Unable to find the '%s' key within the %s event payload.",
                    payload_path_key, event
                )

    def upload_timelapse(self, path, delete_after_upload):
        # just use the path to get the file name.  This requires fewer settings, and a name might not exist
        # for every event we are interested in
        file_name = os.path.basename(path)

        client_secrets = "{}/client_secrets.json".format(self.get_plugin_data_folder())
        credentials_file = "{}/credentials.json".format(self.get_plugin_data_folder())
        storage = Storage(credentials_file)
        credentials = storage.get()

        if credentials is None or credentials.invalid:
            self._logger.info('No Google credentials Defined! Cannot Upload Timelapse %s!', file_name)
            return False

        if credentials.access_token_expired:
            credentials.refresh(httplib2.Http())
            storage = Storage(credentials_file)
            storage.put(credentials)

        youtube = build(YOUTUBE_API_SERVICE_NAME, YOUTUBE_API_VERSION, credentials=credentials)

        tags = None
        if self.tags:
            tags = self.tags.split(",")

        body = dict(
            snippet=dict(
                title=file_name,
                tags=tags,
            ),
            status=dict(
                privacyStatus=self.privacy_status
            )
        )

        # Call the API's videos.insert method to create and upload the video.
        insert_request = youtube.videos().insert(
            part=",".join(body.keys()),
            body=body,
            # The chunksize parameter specifies the size of each chunk of data, in
            # bytes, that will be uploaded at a time. Set a higher value for
            # reliable connections as fewer chunks lead to faster uploads. Set a lower
            # value for better recovery on less reliable connections.
            #
            # Setting "chunksize" equal to -1 in the code below means that the entire
            # file will be uploaded in a single HTTP request. (If the upload fails,
            # it will still be retried where it left off.) This is usually a best
            # practice, but if you're using Python older than 2.6 or if you're
            # running on App Engine, you should set the chunksize to something like
            # 1024 * 1024 (1 megabyte).
            media_body=MediaFileUpload(path, chunksize=-1, resumable=True)
        )

        _status, response = insert_request.next_chunk()
        if response is not None:
            if 'id' in response:
                self._logger.info('Uploaded %s to Youtube!', file_name)
            else:
                self._logger.exception("The upload failed with an unexpected response: %s", response)
                return False

        if delete_after_upload:
            try:
                self._logger.info('Deleting %s from local disk...', file_name)
                os.remove(path)
                self._logger.info('Deleted %s from local disk.', file_name)
            except (OSError, IOError):
                self._logger.exception('Failed to delte %s from local disk.', file_name)
                self._plugin_manager.send_plugin_message(
                    self._identifier, {'type': 'delete-failed', 'file_name': file_name}
                )

        return True

    def get_assets(self):
        return dict(
            js=["js/settings.js"]
        )


__plugin_name__ = "Youtube Timelapse"
__plugin_pythoncompat__ = ">=3,<4"


def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = YoutubeTimelapsePlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information
    }
