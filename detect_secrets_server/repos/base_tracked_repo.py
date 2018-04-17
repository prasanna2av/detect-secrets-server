from __future__ import absolute_import

import os
import subprocess
import sys
from enum import Enum

from detect_secrets.core.baseline import get_secrets_not_in_baseline
from detect_secrets.core.secrets_collection import SecretsCollection
from detect_secrets.plugins import initialize_plugins

from detect_secrets_server.plugins import PluginsConfigParser
from detect_secrets_server.storage.file import FileStorage


class OverrideLevel(Enum):
    NEVER = 0
    ASK_USER = 1
    ALWAYS = 2


class BaseTrackedRepo(object):

    # This should be overriden in subclasses.
    STORAGE_CLASS = FileStorage

    @classmethod
    def initialize_storage(cls, base_directory):
        return cls.STORAGE_CLASS(base_directory)

    def __init__(
            self,
            repo,
            sha,
            plugins,
            baseline_filename,
            exclude_regex,
            cron='',
            base_temp_dir=None,
            **kwargs
    ):
        """
        :type repo: string
        :param repo: git URL or local path of repo

        :type sha: string
        :param sha: last commit hash scanned

        :type plugins: dict
        :param plugins: values to configure various plugins, formatted as
            described in
            detect_secrets_server.plugins.PluginsConfigParser.to_args

        :type base_temp_dir: str
        :param base_temp_dir: the directory to clone git repositories to.

        :type exclude_regex: str
        :param exclude_regex: used for repository scanning; if a filename
            matches this exclude_regex, it is not scanned.

        :type cron: string
        :param cron: crontab syntax, for periodic scanning.

        :type baseline_filename: str
        :param baseline_filename: each repository may have a different
            baseline filename. This allows us to customize these filenames
            per repository.
        """
        self.last_commit_hash = sha
        self.repo = repo
        self.crontab = cron
        self.plugin_config = plugins
        self.baseline_filename = baseline_filename
        self.exclude_regex = exclude_regex

        if base_temp_dir:
            self.storage = self.initialize_storage(base_temp_dir).setup(repo)

    @classmethod
    def load_from_file(
            cls,
            repo_name,
            base_directory,
            *args,
            **kwargs
    ):
        """This will load a TrackedRepo to memory, from a given meta tracked
        file. For automated management without a database.

        The meta tracked file is in the format of self.__dict__

        :type repo_name: string
        :param repo_name: git URL or local path of repo

        :rtype: TrackedRepo
        :raises: FileNotFoundError
        """
        storage = cls.initialize_storage(base_directory)

        data = storage.get(storage.hash_filename(repo_name))
        data = cls.modify_tracked_file_contents(data)

        output = cls(**data)
        output.storage = storage.setup(output.repo)

        return output

    @property
    def name(self):
        return self.storage.repository_name

    def cron(self):
        """Returns the cron command to be appended to crontab"""
        return '%(crontab)s    detect-secrets-server --scan-repo %(name)s' % {
            'crontab': self.crontab,
            'name': self.name,
        }

    def scan(self):
        """Clones the repo, and scans the git diff between last_commit_hash
        and HEAD.

        :raises: subprocess.CalledProcessError
        """
        self.storage.clone_and_pull_master()

        default_plugins = initialize_plugins(self.plugin_config)
        secrets = SecretsCollection(default_plugins, self.exclude_regex)

        try:
            diff = self.storage.get_diff(self.last_commit_hash)
        except subprocess.CalledProcessError:
            self.update()
            return secrets

        secrets.scan_diff(
            diff,
            baseline_filename=self.baseline_filename,
            last_commit_hash=self.last_commit_hash,
            repo_name=self.name,
        )

        baseline = self.storage.get_baseline_file(self.baseline_filename)
        if baseline:
            baseline_collection = SecretsCollection.load_baseline_from_string(baseline)
            secrets = get_secrets_not_in_baseline(secrets, baseline_collection)

        return secrets

    def update(self):
        self.last_commit_hash = self.storage.get_last_commit_hash()

    def save(self, override_level=OverrideLevel.ASK_USER):
        """Saves tracked repo config to file. Returns True if successful.

        :type override_level: OverrideLevel
        :param override_level: determines if we overwrite the JSON file, if exists.
        """
        name = self.name
        if os.path.isfile(self.storage.get_tracked_file_location(
            self.storage.hash_filename(name),
        )):
            if override_level == OverrideLevel.NEVER:
                return False

            elif override_level == OverrideLevel.ASK_USER:
                if not self._prompt_user_override():
                    return False

        self.storage.put(
            self.storage.hash_filename(name),
            self.__dict__,
        )

        return True

    @classmethod
    def modify_tracked_file_contents(cls, data):
        """This function allows us to modify values read from the tracked file,
        before loading it into the class constructor.

        :type data: dict
        :param data: self.__dict__ layout
        :rtype: dict
        """
        data['plugins'] = PluginsConfigParser.from_config(data['plugins']).to_args()

        return data

    @property
    def __dict__(self):
        """This is written to the filesystem, and used in load_from_file.
        Should contain all variables needed to initialize TrackedRepo."""
        output = {
            'sha': self.last_commit_hash,
            'repo': self.repo,
            'plugins': PluginsConfigParser.from_args(self.plugin_config).to_config(),
            'cron': self.crontab,
            'baseline_filename': self.baseline_filename,
            'exclude_regex': self.exclude_regex,
        }

        return output

    def _prompt_user_override(self):  # pragma: no cover
        """Prompts for user input to check if should override file.

        :rtype: bool
        """
        # Make sure to write to stderr, because crontab output is going to be to stdout
        sys.stdout = sys.stderr

        override = None
        while override not in ['y', 'n']:
            override = str(input(
                '"%s" repo already tracked! Do you want to override this (y|n)? ' %
                self.name,
            )).lower()

        sys.stdout = sys.__stdout__

        if override == 'n':
            return False

        return True
