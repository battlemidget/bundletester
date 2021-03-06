import glob
import os
import subprocess
from distutils.spawn import find_executable

import yaml

from bundletester import (config, models, utils)


def normalize_path(path, relto):
    dirname = os.path.dirname(relto)
    if not os.path.isabs(path):
        path = os.path.join(dirname, path)
    return path


def Spec(cmd, parent=None, dirname=None, suite=None, name=None):
    testfile = cmd
    if isinstance(cmd, list):
        testfile = find_executable(cmd[0])
        if not testfile:
            raise OSError(
                "Couldn't find executable for command '%s'" % cmd[0])
        cmd[0] = testfile
    else:
        testfile = os.path.abspath(testfile)
        cmd = [testfile]

    if not os.path.exists(testfile) or \
            not os.access(testfile, os.X_OK | os.R_OK):
        raise OSError('Expected executable test file: %s' % testfile)

    base, ext = os.path.splitext(testfile)
    control_file = "%s.yaml" % base
    if not os.path.exists(control_file):
        control_file = None
    result = config.Parser(path=control_file, parent=parent)
    result['name'] = name or os.path.basename(testfile)
    result['executable'] = cmd
    result['dirname'] = dirname
    result['suite'] = suite
    return result


class Suite(list):
    def __init__(self, model, options, parent_config=None):
        # models.Charm||Bundle -- from SuiteFactory
        # options, argparse options
        self.model = model
        self.options = options
        self._config = None
        self._parent_config = parent_config
        self.directory = model['directory']
        self.testdir = model['testdir']
        self.name = model.get('name')
        if not self.config.bundle:
            self.config.bundle = model.get('bundle')

    def __len__(self):
        l = 0
        for s in self:
            if isinstance(s, Suite):
                l += len(s)
            else:
                l += 1
        return l

    @property
    def config(self):
        if not self._config:
            testcfg = None
            if self._parent_config and self.excluded(self._parent_config):
                # If we're an excluded charm, ignore our tests.yaml. This
                # will avoid installing packages/ppas for a charm that will
                # not be tested.
                pass
            elif self.options.tests_yaml:
                file_path = os.path.abspath(
                    os.path.expanduser(self.options.tests_yaml))
                if os.path.exists(file_path):
                    testcfg = file_path
                else:
                    raise OSError(
                        'Invalid -y argument. File not found: %s' % file_path)
            elif self.testdir:
                testcfg = os.path.join(self.testdir, "tests.yaml")
                if not os.path.exists(testcfg):
                    testcfg = None
            self._config = config.Parser(testcfg, parent=self._parent_config)
        return self._config

    def spec(self, testfile, **kwargs):
        if 'suite' not in kwargs:
            kwargs['suite'] = self
        self.append(Spec(testfile, self.config, **kwargs))

    def excluded(self, config=None):
        config = config or self.config
        if not self.options.exclude:
            self.options.exclude = []
        excludes = set(self.options.exclude).union(set(config.excludes))
        for exclude in excludes:
            if exclude in self.name:
                return True
        return False

    def deploy_cmd(self):
        """Return the bundle deploy command for this suite.

        If the model for this suite is not a Bundle, returns None.
        If 'bundle_deploy' in tests.yaml is False (or falsy), returns None.
        If 'bundle_deploy' in tests.yaml it True, returns a juju-deployer cmd.
        Else returns 'bundle_deploy' from tests.yaml.

        :return: executable command as a list of args

        """
        if not isinstance(self.model, models.Bundle):
            return None
        if not self.config.bundle_deploy:
            return None
        if self.config.bundle_deploy is True:
            # default deploy
            bundle = self.model['bundle'] or self.options.bundle
            if not bundle:
                return None
            if not os.path.exists(bundle):
                raise OSError("Missing required bundle file: %s" % bundle)
            cmd = ['juju-deployer']
            if self.options.verbose:
                cmd.append('-Wvd')
            cmd += ['-c', bundle]
            if self.options.deployment:
                cmd.append(self.options.deployment)
            if self.config.deployment_timeout is not None:
                cmd += ['-t', str(self.config.deployment_timeout)]
            return cmd
        else:
            # self.config.bundle_deploy is a file name
            fullpath = os.path.join(self.testdir, self.config.bundle_deploy)
            if not os.path.isfile(fullpath):
                raise OSError(
                    "'bundle_deploy' in tests.yaml points to a non-existent "
                    "file (%s)" % fullpath)
            if not os.access(fullpath, os.X_OK | os.R_OK):
                raise OSError(
                    "'bundle_deploy' file must be +rx (%s)" % fullpath)
            return [fullpath]

    def find_tests(self):
        if not self.testdir:
            return
        if self.excluded():
            return
        testpat = self.options.test_pattern or \
            self.config.get('tests', 'test*')
        tests = set(glob.glob(os.path.join(self.testdir, testpat)))
        if self.options.tests:
            filterset = [os.path.join(self.testdir, f) for f
                         in self.options.tests]
            tests = tests.intersection(set(filterset))

        exec_tests = []
        for test in sorted(tests):
            if os.path.isfile(test) and os.access(test, os.X_OK | os.R_OK):
                exec_tests.append(test)
                self.spec(test, dirname=self.model['directory'], suite=self)

        # When a test pattern is provided (other than the default), expect
        # at least one executable glob match, otherwise fail.
        if (testpat and
                testpat != config.Parser().__defaults__()['tests'] and not
                exec_tests):
            raise OSError('Expected at least one executable pattern '
                          'match for: {}'.format(testpat))

        # When one or more test name arguments are provided, expect ALL
        # arguments to exist as executable files, otherwise fail.
        if self.options.tests and len(self.options.tests) != len(exec_tests):
            raise OSError('Expected executable test files: '
                          '{}'.format(self.options.tests))

    def find_suite(self):
        """Find and prepend charms tests to our suite of tests.
        bundle: path to bundle file
        deployment: name of deployment in bundle

        If only one target exists and deployment is not specified it will be
        used automatically when searching for tests.
        """
        if self.excluded():
            return
        if isinstance(self.model, (models.Bundle, models.Charm)):
            if not self.options.skip_implicit:
                self.find_implicit_tests()

        if isinstance(self.model, models.Bundle):
            deployment = utils.fetch_deployment(self.config.bundle,
                                                self.options.deployment)
            for charm in deployment.get_charms():
                model = models.Charm.from_deployer_charm(charm)
                charm_suite = Suite(model, self.options,
                                    parent_config=self.config)
                charm_suite.find_suite()
                if len(charm_suite):
                    self.insert(0, charm_suite)
        self.find_tests()

    def conditional_make(self, target, entitydir, suite=None):
        cwd = os.getcwd()
        os.chdir(entitydir)
        p = subprocess.Popen(['make', '-ns', target],
                             stdout=open('/dev/null', 'w'),
                             stderr=subprocess.STDOUT)
        ec = p.wait()
        if ec == 0:
            # The makefile target exists, add the spec
            self.spec(['make', '-s', target],
                      name="make %s" % target,
                      dirname=entitydir,
                      suite=suite)
        os.chdir(cwd)

    def find_implicit_tests(self):
        # Look for implicit targets and map these as tests
        # for charms this can include 'charm proof' from
        # charm tools and for bundles and charms with Makefiles
        # common targets will be attempted.
        # This is a charm suite
        proof = ['charm-proof']
        self.spec(proof,
                  dirname=self.model['directory'], suite=self)
        for target in (self.config.makefile or []):
            self.conditional_make(target, self.model['directory'],
                                  suite=self)


def filter_yamls(yamls):
    """Look at a series of *.yaml files to see if they
    might be deployer files. Return the filtered list.
    """
    if not yamls:
        return

    result = []
    for yamlfn in yamls:
        data = yaml.safe_load(open(yamlfn))
        if not isinstance(data, dict):
            continue
        # v4 bundle format
        if 'services' in data and 'services' not in data['services']:
            result.append(yamlfn)
            continue
        for possible in data.values():
            if isinstance(possible, dict) and 'services' in possible:
                keys = sorted(possible['services'].keys())
                if keys == ['default', 'description', 'type']:
                    # looks like a charm, not a bundle
                    continue
                # v3 bundle format
                result.append(yamlfn)
                break
    return result


def find_bundle_file(directory, bundle, filter_yamls=filter_yamls):
    if bundle is not None:
        bp = os.path.join(directory, bundle)
        if not os.path.exists(bp):
            raise OSError("%s not found" % bp)
        return bp
    pat = os.path.join(directory, "*.yaml")
    yamls = glob.glob(pat)
    yamls = filter_yamls(yamls)
    if not yamls:
        return
    if len(yamls) > 1:
        raise OSError(
            "Ambigious bundle options: %s. Disambiguate with --bundle" % yamls)
    return yamls[0]


def BundleClassifier(directory, options):
    bundle = find_bundle_file(directory, options.bundle)
    if not bundle:
        return None
    return models.Bundle({
        'bundle': bundle,
        'testdir': utils.find_testdir(directory),
        'name': 'bundle',
    })


def CharmClassifier(directory, options):
    metadata = os.path.join(directory, "metadata.yaml")
    if not os.path.exists(metadata):
        return None
    testdir = utils.find_testdir(directory)
    metadata = yaml.safe_load(open(metadata))
    return models.Charm({
        'metadata': metadata,
        'testdir': testdir,
        'name': metadata['name'],
    })


def TestDirClassifier(directory, options):
    if not os.path.exists(directory):
        return None
    return models.TestDir({
        'testdir': directory,
        'name': os.path.basename(directory)
    })


def SuiteFactory(options, directory="."):
    """Return a Suite for a given directory.

    This classifies the dir based on a series of tests.
    """
    for classifier in [BundleClassifier, CharmClassifier, TestDirClassifier]:
        model = classifier(directory, options)
        if model:
            model['directory'] = directory
            suite = Suite(model, options)
            suite.find_suite()
            return suite
    return None
