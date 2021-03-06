# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Copyright (c) 2014-2018, Lars Asplund lars.anders.asplund@gmail.com

"""
Contains classes to represent a test bench and test cases
"""

import logging
from os.path import basename
import re
import bisect
from collections import OrderedDict
from vunit.ostools import file_exists
from vunit.cached import cached
from vunit.test_list import TestList
from vunit.vhdl_parser import remove_comments as remove_vhdl_comments
from vunit.test_suites import IndependentSimTestCase, SameSimTestSuite
from vunit.parsing.encodings import HDL_FILE_ENCODING
from vunit.project import file_type_of, VERILOG_FILE_TYPES
from vunit.configuration import Configuration, ConfigurationVisitor, DEFAULT_NAME


LOGGER = logging.getLogger(__name__)


class TestBench(ConfigurationVisitor):
    """
    A VUnit test bench top level
    """

    def __init__(self, design_unit, database=None):
        ConfigurationVisitor.__init__(self)
        self.design_unit = design_unit
        self._database = database

        self._individual_tests = False
        self._configs = {}
        self._test_cases = []
        self._implicit_test = None

        if design_unit.is_entity:
            design_unit.set_add_architecture_callback(self._add_architecture_callback)
            if design_unit.architecture_names:
                self._add_architecture_callback()
        else:
            self.scan_tests_from_file(design_unit.file_name)

    def _add_architecture_callback(self):
        """
        Called when architectures have been added
        """
        self._check_architectures(self.design_unit)
        file_name = list(self.design_unit.architecture_names.values())[0]
        self.scan_tests_from_file(file_name)

    @property
    def name(self):
        return self.design_unit.name

    @property
    def library_name(self):
        return self.design_unit.library_name

    @property
    def tests(self):
        return self._test_cases

    def get_default_config(self):
        """
        Get the default configuration of this test bench
        """
        if self._individual_tests:
            raise RuntimeError("Test bench %s.%s has individually configured tests"
                               % (self.library_name, self.name))
        return self._configs[DEFAULT_NAME]

    @staticmethod
    def _check_architectures(design_unit):
        """
        Check that an entity which has been classified as a VUnit test bench
        has exactly one architecture. Raise RuntimeError otherwise.
        """
        if design_unit.is_entity:
            if not design_unit.architecture_names:
                raise RuntimeError("Test bench '%s' has no architecture."
                                   % design_unit.name)

            elif len(design_unit.architecture_names) > 1:
                raise RuntimeError("Test bench not allowed to have multiple architectures. "
                                   "Entity %s has %s"
                                   % (design_unit.name,
                                      ", ".join("%s:%s" % (name, basename(fname))
                                                for name, fname in sorted(design_unit.architecture_names.items()))))

    def create_tests(self, simulator_if, elaborate_only, test_list=None):
        """
        Create all test cases from this test bench
        """

        self._check_architectures(self.design_unit)

        if test_list is None:
            test_list = TestList()

        if self._individual_tests:
            for test_case in self._test_cases:
                test_case.create_tests(simulator_if, elaborate_only, test_list)
        elif self._implicit_test:
            for config in self._get_configurations_to_run():
                test_list.add_test(
                    IndependentSimTestCase(
                        test=self._implicit_test,
                        config=config,
                        simulator_if=simulator_if,
                        elaborate_only=elaborate_only))
        else:
            for config in self._get_configurations_to_run():
                test_list.add_suite(
                    SameSimTestSuite(
                        tests=[test.test for test in self._test_cases],
                        config=config,
                        simulator_if=simulator_if,
                        elaborate_only=elaborate_only))
        return test_list

    @property
    def test_case_names(self):
        return [test.name for test in self._test_cases]

    def get_test_case(self, name):
        """
        Return the test case with name or raise KeyError
        """
        for test_case in self._test_cases:
            if test_case.name == name:
                return test_case
        raise KeyError(name)

    def get_configuration_dicts(self):
        """
        Get all configurations within the test bench

        If running all tests in the same simulation there are no individual test configurations
        """
        if self._individual_tests:
            configs = []
            for test_case in self._test_cases:
                configs += test_case.get_configuration_dicts()
            return configs

        return [self._configs]

    def _get_configurations_to_run(self):
        """
        Get all simulation runs for this test bench
        """
        configs = self._configs.copy()
        if len(configs) > 1:
            # Remove default configurations when there are more than one
            del configs[DEFAULT_NAME]
        return configs.values()

    def scan_tests_from_file(self, file_name):
        """
        Scan file for test cases and pragmas
        """
        if not file_exists(file_name):
            raise ValueError("File %r does not exist" % file_name)

        def parse(content):
            """
            Parse pragmas and test case names
            """
            pragmas = _find_pragmas(content, file_name)
            tests = _find_tests(content, file_name)
            return pragmas, tests

        pragmas, tests = cached("test_bench.parse",
                                parse,
                                file_name,
                                encoding=HDL_FILE_ENCODING,
                                database=self._database)

        default_config = Configuration(DEFAULT_NAME, self.design_unit)

        if "fail_on_warning" in pragmas:
            default_config.set_sim_option("vhdl_assert_stop_level", "warning")

        self._configs = OrderedDict({default_config.name: default_config})

        explicit_tests = [test for test in tests if test.is_explicit]
        if explicit_tests:
            # All tests shall be explicit when there are at least one explicit test
            assert len(tests) == len(explicit_tests)
            self._implicit_test = None
        else:
            # There can only be one implicit test
            assert len(tests) == 1
            self._implicit_test = tests[0]

        self._individual_tests = "run_all_in_same_sim" not in pragmas and len(explicit_tests) > 0
        self._test_cases = [TestConfigurationVisitor(test,
                                                     self.design_unit,
                                                     self._individual_tests,
                                                     default_config.copy())
                            for test in explicit_tests]


class Test(object):
    """
    Holds information about a test in the source code

    - name of test
    - file name and line number where test came from
    - if it was an explicit or implicit test [1]

    [1]: Explicit tests are those where the user has written run("test name").
         Implicit tests are those when there are no tests in the test bench, just the test suite
    """

    def __init__(self, name, file_name, lineno):
        self._name = name
        self._file_name = file_name
        self._lineno = lineno

    @property
    def name(self):
        return self._name

    @property
    def file_name(self):
        return self._file_name

    @property
    def lineno(self):
        return self._lineno

    @property
    def is_explicit(self):
        return self._name is not None


class TestConfigurationVisitor(ConfigurationVisitor):
    """
    A means to creates configurations for single test
    """
    def __init__(self, test, design_unit, enable_configuration, default_config):
        ConfigurationVisitor.__init__(self)
        self._test = test
        assert test.is_explicit
        self.design_unit = design_unit
        self._enable_configuration = enable_configuration
        self._configs = OrderedDict({default_config.name: default_config})

    @property
    def name(self):
        return self._test.name

    @property
    def test(self):
        return self._test

    def get_default_config(self):
        """
        Get the default configuration of this test case
        """
        self._check_enabled()
        return self._configs[DEFAULT_NAME]

    def _check_enabled(self):
        if not self._enable_configuration:
            raise RuntimeError("Individual test configuration is not possible with run_all_in_same_sim")

    def get_configuration_dicts(self):
        """
        Get all configurations of this test
        """
        return [self._configs]

    def _get_configurations_to_run(self):
        """
        Get all simulation runs for this test bench
        """
        configs = self._configs.copy()
        if len(configs) > 1:
            # Remove default configurations when there are more than one
            del configs[DEFAULT_NAME]
        return configs.values()

    def create_tests(self, simulator_if, elaborate_only, test_list=None):
        """
        Create all tests from this test case which may be several depending on the number of configurations
        """
        for config in self._get_configurations_to_run():
            test_list.add_test(
                IndependentSimTestCase(
                    test=self._test,
                    config=config,
                    simulator_if=simulator_if,
                    elaborate_only=elaborate_only))


_RE_VHDL_TEST_CASE = re.compile(r'(\s|\()+run\s*\(\s*"(?P<name>.*?)"\s*\)', re.IGNORECASE)
_RE_VERILOG_TEST_CASE = re.compile(r'`TEST_CASE\s*\(\s*"(?P<name>.*?)"\s*\)')
_RE_VHDL_TEST_SUITE = re.compile(r'test_runner_setup\s*\(', re.IGNORECASE)
_RE_VERILOG_TEST_SUITE = re.compile(r'`TEST_SUITE\b')


def _get_line_offsets(code):
    """
    Returns a list with one entry per line returning the offset in the
    code where it starts
    """

    offset = 0
    offsets = []
    for line in code.splitlines():
        offsets.append(offset)
        offset += len(line) + 1

    return offsets


def _lookup_lineno(offset, offsets):
    """
    Convert offset into line number
    """
    return bisect.bisect(offsets, offset)


def _check_duplicate_tests(tests):
    """
    Check for duplicate tests and raise RuntimeError
    """
    known_tests = {}
    found_duplicates = False
    for test in tests:
        if test.name in known_tests:
            known_test = known_tests[test.name]
            LOGGER.error('Duplicate test "%s" in %s line %i previously defined on line %i',
                         test.name,
                         test.file_name, test.lineno,
                         known_test.lineno)
            found_duplicates = True
        else:
            known_tests[test.name] = test

    if found_duplicates:
        raise RuntimeError('Duplicate tests where found')


def _find_tests(code, file_name):
    """
    Finds all tests within a file including implicit tests where there
    is only a test suite

    returns a list to Test objects
    """
    is_verilog = file_type_of(file_name) in VERILOG_FILE_TYPES
    line_offsets = _get_line_offsets(code)

    if is_verilog:
        code = _remove_verilog_comments(code)
        regexp = _RE_VERILOG_TEST_CASE
        suite_regexp = _RE_VERILOG_TEST_SUITE
    else:
        code = remove_vhdl_comments(code)
        regexp = _RE_VHDL_TEST_CASE
        suite_regexp = _RE_VHDL_TEST_SUITE

    tests = [Test(name=match.group("name"),
                  file_name=file_name,
                  lineno=_lookup_lineno(match.start("name"), line_offsets))
             for match in regexp.finditer(code)]

    _check_duplicate_tests(tests)

    if not tests:
        # Implicit test, use the test suite start as lineno
        match = suite_regexp.search(code)

        if match:
            lineno = _lookup_lineno(match.start(0), line_offsets)
        else:
            LOGGER.warning("Found no tests or test suite within %s", file_name)
            lineno = 1

        tests = [Test(None,
                      file_name=file_name,
                      lineno=lineno)]

    return tests


_RE_PRAGMA = re.compile(r'vunit_pragma\s+([a-zA-Z0-9_]+)', re.IGNORECASE)
_VALID_PRAGMAS = ["run_all_in_same_sim", "fail_on_warning"]


def _find_pragmas(code, file_name):
    """
    Return a list of all vunit pragmas parsed from the code

    @TODO only look inside comments
    """
    pragmas = []
    for match in _RE_PRAGMA.finditer(code):
        pragma = match.group(1)
        if pragma not in _VALID_PRAGMAS:
            LOGGER.warning("Invalid pragma '%s' in %s",
                           pragma,
                           file_name)
        pragmas.append(pragma)
    return pragmas


VERILOG_REMOVE_COMMENT_RE = re.compile(r'(//[^\n]*)|(/\*.*?\*/)',
                                       re.DOTALL)


def _comment_repl(match):
    """
    Replace comment with equal amount of whitespace to make
    lexical position unaffected
    """
    text = match.group(0)
    return "".join(" " if c != "\n" else "\n"
                   for c in text)


def _remove_verilog_comments(code):
    """
    Remove all verilog comments
    """
    return VERILOG_REMOVE_COMMENT_RE.sub(_comment_repl, code)
