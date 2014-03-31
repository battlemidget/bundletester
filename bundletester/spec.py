import glob
import os
from bundletester import config


def loader(testfile, parent=None):
    result = config.Parser()
    if not os.path.exists(testfile) or \
            not os.access(testfile, os.X_OK | os.R_OK):
        raise OSError('Expected executable test file: %s' % testfile)

    result['name'] = os.path.basename(testfile)
    result['executable'] = os.path.abspath(testfile)

    base, ext = os.path.splitext(testfile)
    control_file = "%s.yaml" % base
    if not os.path.exists(control_file):
        control_file = None
    result['config'] = config.Parser(path=control_file, parent=parent)
    return result


class Spec(config.Parser):
    def __init__(self, testfile, parent):
        data = loader(testfile, parent)
        self.update(data)


class Suite(list):
    def __init__(self, config):
        self.config = config

    def spec(self, testfile):
        self.append(Spec(testfile, self.config))

    def find_tests(self, bundledir, filterset=None):
        tests = set(glob.glob(os.path.join(bundledir, 'test*')))
        if filterset:
            filterset = [os.path.join(bundledir, f) for f in filterset]
            tests = tests.intersection(set(filterset))
        for test in sorted(tests):
            if os.access(test, os.X_OK):
                self.spec(test)
