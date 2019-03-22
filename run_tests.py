#!/usr/bin/env python

import logging
import tempfile
import shutil
import unittest
import os

import ipa


def get_path_to_resource_file(tc_name, file_name):
    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), 'tests', tc_name, file_name))


class _BaseTestCase(unittest.TestCase):
    """Extend unittest.TestCase with more functionality"""
    def __init__(self, *args, **kwargs):
        super(_BaseTestCase, self).__init__(*args, **kwargs)
        self.addTypeEqualityFunc(str, self.assertEqualWithDiff)

    def assertEqualWithDiff(self, left, right, msg=None):
        import difflib
        try:
            self._baseAssertEqual(left, right)
        except self.failureException:
            diff = difflib.unified_diff(
                left.splitlines(True),
                right.splitlines(True),
                n=0
            )
            diff = ''.join(diff)
            raise self.failureException("{0}\n{1}".format(msg or '', diff))


class IpaTest(_BaseTestCase):

    def setUp(self):
        # show all differences
        self.maxDiff = None

        # disable logging temporarily to reduce spam
        logging.getLogger().setLevel(logging.WARNING)

    def test_basic_text_output(self):
        self.run_test('basic', 'human')

    def test_basic_json_output(self):
        self.run_test('basic', 'json')

    def test_basic_yaml_anchors_output(self):
        self.run_test('basic', 'yaml-anchors')

    def run_test(self, tc_name, output_format):
        if output_format == 'human':
            ofile_name = 'output.txt'
        elif output_format == 'json':
            ofile_name = 'output.json'
        elif output_format == 'yaml-anchors':
            ofile_name = 'output.yaml'
        else:
            raise NotImplementedError

        input_file = get_path_to_resource_file(tc_name, 'input.yaml')
        output_file = get_path_to_resource_file(tc_name, ofile_name)

        # create the list of arguments
        args = [
            input_file,
            '-o', output_format
        ]

        res = ipa.main(args)

        # compare the content of the output file
        # to the content of the expected file
        with open(output_file) as f:
            exp = f.read()
        self.assertEqualWithDiff(exp.strip(), res.strip())


if __name__ == "__main__":
    suite = unittest.TestSuite()
    suite.addTest(unittest.TestLoader().loadTestsFromTestCase(IpaTest))
    unittest.TextTestRunner().run(suite)
