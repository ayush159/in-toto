#!/usr/bin/env python

"""
<Program Name>
  test_verifylib.py

<Author>
  Lukas Puehringer <lukas.puehringer@nyu.edu>

<Started>
  Nov 07, 2016

<Copyright>
  See LICENSE for licensing information.

<Purpose>
  Test verifylib functions.

"""

import os
import shutil
import copy
import tempfile
import unittest
import glob
import shlex
from mock import patch
from datetime import datetime
from dateutil.relativedelta import relativedelta

import in_toto.settings
from in_toto.models.metadata import Metablock
from in_toto.models.link import Link, FILENAME_FORMAT
from in_toto.models.layout import (Step, Inspection, Layout,
    SUBLAYOUT_LINK_DIR_FORMAT)
from in_toto.verifylib import (verify_delete_rule, verify_create_rule,
    verify_modify_rule, verify_allow_rule, verify_disallow_rule,
    verify_match_rule, verify_item_rules, verify_all_item_rules,
    verify_command_alignment, run_all_inspections, in_toto_verify,
    verify_sublayouts, get_summary_link, _raise_on_bad_retval,
    load_links_for_layout, verify_link_signature_thresholds,
    verify_threshold_constraints)
from in_toto.exceptions import (RuleVerificationError,
    SignatureVerificationError, LayoutExpiredError, BadReturnValueError,
    ThresholdVerificationError)
from in_toto.util import import_rsa_key_from_file, import_public_keys_from_files_as_dict
from in_toto.rulelib import unpack_rule
import in_toto.gpg.functions

import securesystemslib.exceptions
import in_toto.exceptions


class Test_RaiseOnBadRetval(unittest.TestCase):
  """Tests internal function that raises an exception if the passed
  "return_value" is not and integer and not zero. """

  def test_zero_return_value(self):
    """Don't raise exception on zero return value. """
    _raise_on_bad_retval(0)
    _raise_on_bad_retval(0, "command")

  def test_non_int_return_value(self):
    """Raise exception on non-int return value. """
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval("bad retval")
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval("bad retval", "bad command")

  def test_non_zero_return_value(self):
    """Raise exception on non-zero return value. """
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval(1)
    with self.assertRaises(BadReturnValueError):
      _raise_on_bad_retval(-1, "bad command")


class TestRunAllInspections(unittest.TestCase):
  """Test verifylib.run_all_inspections(layout)"""

  @classmethod
  def setUpClass(self):
    """
    Create layout with dummy inpsection.
    Create and change into temp test directory with dummy artifact."""

    # find where the scripts directory is located.
    scripts_directory = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "scripts")

    # Create layout with one inspection
    self.layout = Layout.read({
        "_type": "layout",
        "steps": [],
        "inspect": [{
          "name": "touch-bar",
          "run": ["python", os.path.join(scripts_directory, "touch"), "bar"],
        }]
      })

    # Create directory where the verification will take place
    self.working_dir = os.getcwd()
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)
    open("foo", "w").write("foo")

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp test directory. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_inpsection_artifacts_with_base_path_ignored(self):
    """Create new dummy test dir and set as base path, must ignore. """
    ignore_dir = os.path.realpath(tempfile.mkdtemp())
    ignore_foo = os.path.join(ignore_dir, "ignore_foo")
    open(ignore_foo, "w").write("ignore foo")
    in_toto.settings.ARTIFACT_BASE_PATH = ignore_dir

    run_all_inspections(self.layout)
    link = Metablock.load("touch-bar.link")
    self.assertListEqual(list(link.signed.materials.keys()), ["foo"])
    self.assertListEqual(sorted(list(link.signed.products.keys())), sorted(["foo", "bar"]))

    in_toto.settings.ARTIFACT_BASE_PATH = None
    shutil.rmtree(ignore_dir)

  def test_inspection_fail_with_non_zero_retval(self):
    """Test fail run inspections with non-zero return value. """
    layout = Layout.read({
        "_type": "layout",
        "steps": [],
        "inspect": [{
          "name": "non-zero-inspection",
          "run": ["python", "./scripts/expr", "1", "/", "0"],
        }]
    })
    with self.assertRaises(BadReturnValueError):
      run_all_inspections(layout)


class TestVerifyCommandAlignment(unittest.TestCase):
  """Test verifylib.verify_command_alignment(command, expected_command)"""

  def setUp(self):
    self.command = ["vi", "file1", "file2"]

  def test_commands_align(self):
    """Cmd and expected cmd are equal, passes. """
    expected_command = ["vi", "file1", "file2"]
    verify_command_alignment(self.command, expected_command)

  def test_commands_do_not_fully_align_log_warning(self):
    """Cmd and expected cmd differ slightly. """
    expected_command = ["/usr/bin/vi", "file1", "file2"]

    with patch("in_toto.verifylib.log") as mock_logging:
      verify_command_alignment(self.command, expected_command)
      mock_logging.warning.assert_called_with("Run command '{0}'"
          " differs from expected command '{1}'"
          .format(self.command, expected_command))

  def test_commands_do_not_align_at_all_log_warning(self):
    """Cmd and expected cmd differ completely. """
    expected_command = ["make install"]

    with patch("in_toto.verifylib.log") as mock_logging:
      verify_command_alignment(self.command, expected_command)
      mock_logging.warning.assert_called_with("Run command '{0}'"
          " differs from expected command '{1}'"
          .format(self.command, expected_command))


class TestVerifyRule(unittest.TestCase):
  """Table driven tests for individual rule verification functions. """

  def test_verify_delete_rule(self):
    """Test verifylib.verify_delete_rule. """
    test_data_keys = [
        "rule pattern", "artifact queue", "materials", "products", "expected"]
    test_cases = [
      # Consume deleted artifact
      ["foo", {"foo"}, {"foo"}, set(), {"foo"}],
      # Consume multiple deleted artifacts with wildcard
      ["*", {"foo", "bar"}, {"foo", "bar"}, set(), {"foo", "bar"}],
      # Don't consume created artifact (in products only)
      ["foo", {"foo"}, set(), {"foo"}, set()],
      # Don't consume artifact that's not in materials or products
      # NOTE: In real life this shouldn't be in the queue either
      ["foo", {"foo"}, set(), set(), set()],
      # Don't consume deleted but not queued artifact
      ["foo", set(), {"foo"}, set(), set()],
      # Don't consume deleted but not matched artifact
      ["bar", {"foo"}, {"foo"}, set(), set()]
    ]

    for i, test_data in enumerate(test_cases):
      pattern, queue, materials, products, expected = test_data
      result = verify_delete_rule(pattern, queue, materials, products)
      self.assertSetEqual(result, expected,
          "test {}: {}".format(i, dict(zip(test_data_keys, test_data))))


  def test_verify_create_rule(self):
    """Test verifylib.verify_create_rule. """
    test_data_keys = [
        "rule pattern", "artifact queue", "materials", "products", "expected"]
    test_cases = [
      # Consume created artifact
      ["foo", {"foo"}, set(), {"foo"}, {"foo"}],
      # Consume multiple created artifacts with wildcard
      ["*", {"foo", "bar"},  set(), {"foo", "bar"}, {"foo", "bar"}],
      # Don't consume deleted artifact (in materials only)
      ["foo", {"foo"}, {"foo"}, set(), set()],
      # Don't consume artifact that's not in materials or products
      # NOTE: In real life this shouldn't be in the queue either
      ["foo", {"foo"}, set(), set(), set()],
      # Don't consume created but not queued artifact
      ["foo", set(), set(), {"foo"}, set()],
      # Don't consume created but not matched artifact
      ["bar", {"foo"}, set(), {"foo"}, set()]
    ]

    for i, test_data in enumerate(test_cases):
      pattern, queue, materials, products, expected = test_data
      result = verify_create_rule(pattern, queue, materials, products)
      self.assertSetEqual(result, expected,
          "test {}: {}".format(i, dict(zip(test_data_keys, test_data))))


  def test_verify_modify_rule(self):
    """Test verifylib.verify_modify_rule. """
    sha_a = "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    sha_b = "155c693a6b7481f48626ebfc545f05236df679f0099225d6d0bc472e6dd21155"

    test_data_keys = [
        "rule pattern", "artifact queue", "materials", "products", "expected"]
    test_cases = [
      # Consume modified artifact
      ["foo", {"foo"}, {"foo": {"sha256": sha_a}}, {"foo": {"sha256": sha_b}},
          {"foo"}],
      # Consume multiple modified artifacts with wildcard
      ["*", {"foo", "bar"},
          {"foo": {"sha256": sha_a}, "bar": {"sha256": sha_a}},
          {"foo": {"sha256": sha_b}, "bar": {"sha256": sha_b}},
          {"foo", "bar"}],
      # Don't consume unmodified artifact
      ["foo", {"foo"}, {"foo": {"sha256": sha_a}}, {"foo": {"sha256": sha_a}},
          set()],
      # Don't consume artifact that's not in materials or products
      # NOTE: In real life this shouldn't be in the queue either
      ["foo", {"foo"}, {}, {}, set()],
      # Don't consume modified but not queued artifact
      ["foo", set(), {"foo": {"sha256": sha_a}}, {"foo": {"sha256": sha_b}},
          set()],
      # Don't consume modified but not matched artifact
      ["bar", {"foo"}, {"foo": {"sha256": sha_a}}, {"foo": {"sha256": sha_b}},
          set()],
    ]

    for i, test_data in enumerate(test_cases):
      pattern, queue, materials, products, expected = test_data
      result = verify_modify_rule(pattern, queue, materials, products)
      self.assertSetEqual(result, expected,
          "test {}: {}".format(i, dict(zip(test_data_keys, test_data))))


  def test_verify_allow_rule(self):
    """Test verifylib.verify_allow_rule. """
    test_data_keys = ["rule pattern", "artifact queue", "expected"]
    test_cases = [
      # Consume allowed artifact
      ["foo", {"foo"}, {"foo"}],
      # Consume multiple allowed artifacts with wildcard
      ["*", {"foo", "bar"}, {"foo", "bar"}],
      # Consume multiple allowed artifacts with wildcard 2
      ["foo*", {"foo", "foobar", "bar"}, {"foo", "foobar"}],
      # Don't consume unmatched artifacts
      ["bar", {"foo"}, set()],
      # Don't consume artifacts if nothing is in the queue
      ["foo", set(), set()],

    ]
    for i, test_data in enumerate(test_cases):
      pattern, queue, expected = test_data
      result = verify_allow_rule(pattern, queue)
      self.assertSetEqual(result, expected,
          "test {}: {}".format(i, dict(zip(test_data_keys, test_data))))


  def test_verify_disallow_rule(self):
    """Test verifylib.verify_disallow_rule. """
    test_data_keys = ["rule pattern", "artifact queue"]
    test_cases = [
      # Foo disallowed, raise
      ["foo", {"foo"}, True],
      # All disallowed, raise
      ["*", {"foo", "bar"}, True],
      # Foo disallowed, but only bar there, don't raise
      ["foo", {"bar"}, False],
      # All disallowed, but no artifacts, don't raise
      ["*", {}, False]
    ]

    for i, test_data in enumerate(test_cases):
      pattern, queue, should_raise = test_data

      msg = "test {}: {}".format(i, dict(zip(test_data_keys, test_data)))
      exception = None

      try:
        verify_disallow_rule(pattern, queue)
      except RuleVerificationError as e:
        exception = e

      if should_raise and not exception:
         self.fail("Expected 'RuleVerificationError'\n{}".format(msg))

      if exception and not should_raise:
          self.fail("Unexpected {}\n{}".format(exception, msg))


class TestVerifyMatchRule(unittest.TestCase):
  """Test verifylib.verify_match_rule(rule, artifact_queue, artifacts, links) """

  def setUp(self):
    """Setup artifact queues, artifacts dictionary and Link dictionary. """

    # Dummy artifact hashes
    self.sha256_foo = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    self.sha256_foobar = \
        "155c693a6b7481f48626ebfc545f05236df679f0099225d6d0bc472e6dd21155"
    self.sha256_bar = \
        "cfdaaf1ab2e4661952a9dec5e8fa3c360c1b06b1a073e8493a7c46d2af8c504b"
    self.sha256_barfoo = \
        "2036784917e49b7685c7c17e03ddcae4a063979aa296ee5090b5bb8f8aeafc5d"

    # Link dictionary containing dummy artifacts related to Steps the rule is
    # matched with (match destination).
    self.materials = {
      "foo": {"sha256": self.sha256_foo},
      "foobar": {"sha256": self.sha256_foobar},
      "sub/foo": {"sha256": self.sha256_foo},
      "sub/foobar": {"sha256": self.sha256_foobar}

    }
    self.products = {
      "bar": {"sha256": self.sha256_bar},
      "barfoo": {"sha256": self.sha256_barfoo},
      "sub/bar": {"sha256": self.sha256_bar},
      "sub/barfoo": {"sha256": self.sha256_barfoo},
      }

    self.links = {
        "dest-item": Metablock(signed=Link(
            name="dest-item",
            materials=self.materials,
            products=self.products)),
    }


  def test_verify_match_rule(self):
    test_data_keys = [
        "rule string", "artifacts queue", "source artifacts", "expected"]
    test_cases = [
      [
        # Consume foo matching with dest material foo
        "MATCH foo WITH MATERIALS FROM dest-item",
        set(self.materials.keys()), self.materials,
        {"foo"}
      ],
      [
        # Consume foo matching with dest product foo
        "MATCH bar WITH PRODUCTS FROM dest-item",
        set(self.products.keys()), self.products,
        {"bar"}
      ],
      [
        # Consume sub/foo matching with dest material foo
        "MATCH foo IN sub WITH MATERIALS FROM dest-item",
        set(self.materials.keys()), self.materials,
        {"sub/foo"}
      ],
      [
        # Consume sub/foo matching with dest material foo (ignore trailing /)
        "MATCH foo IN sub/ WITH MATERIALS FROM dest-item",
        set(self.materials.keys()), self.materials,
        {"sub/foo"}
      ],
      [
        # Consume sub/bar matching with dest product bar
        "MATCH bar IN sub WITH PRODUCTS FROM dest-item",
        set(self.products.keys()), self.products,
        {"sub/bar"}
      ],
      [
        # Consume foo matching with dest material sub/foo
        "MATCH foo WITH MATERIALS IN sub FROM dest-item",
        set(self.materials.keys()), self.materials,
        {"foo"}
      ],
      [
        # Consume bar matching with dest product sub/bar
        "MATCH bar WITH PRODUCTS IN sub FROM dest-item",
        set(self.products.keys()), self.products,
        {"bar"}
      ],
      [
        # Consume bar matching with dest product sub/bar (ignore trailing /)
        "MATCH bar WITH PRODUCTS IN sub/ FROM dest-item",
        set(self.products.keys()), self.products,
        {"bar"}
      ],
      [
        # Consume foo* matching with dest material foo*
        "MATCH foo* WITH MATERIALS FROM dest-item",
        set(self.materials.keys()), self.materials,
        {"foo", "foobar"}
      ],
      [
        # Consume sub/foo* matching with dest material foo*
        "MATCH foo* IN sub WITH MATERIALS FROM dest-item",
        set(self.materials.keys()), self.materials,
        {"sub/foo", "sub/foobar"}
      ],
      [
        # Consume bar* matching with dest product bar*
        "MATCH bar* WITH PRODUCTS FROM dest-item",
        set(self.products.keys()), self.products,
        {"bar", "barfoo"}
      ],
      [
        # Consume bar* matching with dest product sub/bar*
        "MATCH bar* WITH PRODUCTS IN sub FROM dest-item",
        set(self.products.keys()), self.products,
        {"bar", "barfoo"}
      ],
      [
        # Don't consume (empty queue)
        "MATCH foo WITH MATERIALS FROM dest-item",
        set(), self.materials,
        set()
      ],
      [
        # Don't consume (no destination artifact)
        "MATCH foo WITH PRODUCTS FROM dest-item",
        set(self.materials.keys()), self.materials,
        set()
      ],
      [
        # Don't consume (non-matching hashes)
        "MATCH foo WITH MATERIALS FROM dest-item",
        {"foo"}, {"foo": {"sha256": "deadbeef"}},
        set()
      ],
      [
        # Don't consume (missing link)
        "MATCH foo WITH MATERIALS FROM dest-item-missing-link",
        set(self.materials.keys()), self.materials,
        set()
      ]
    ]

    for i, test_data in enumerate(test_cases):
      rule_string, queue, source_artifacts, expected = test_data

      # Generate rule data from rule string
      rule_data = unpack_rule(shlex.split(rule_string))

      result = verify_match_rule(
          rule_data, queue, source_artifacts, self.links)

      self.assertSetEqual(result, expected,
          "'result': {}\n test {}: {}, 'links':{}".format(result,
          i, dict(zip(test_data_keys, test_data)), self.links))



class TestVerifyItemRules(unittest.TestCase):
  """Test verifylib.verify_item_rules(source_name, source_type, rules, links)"""

  def setUp(self):
    self.item_name = "item"
    self.sha256_1 = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    self.sha256_2 = \
        "cfdaaf1ab2e4661952a9dec5e8fa3c360c1b06b1a073e8493a7c46d2af8c504b"

    self.links = {
      "item": Metablock(signed=Link(name="item",
          materials={
              "foo": {"sha256": self.sha256_1},
              "foobar": {"sha256": self.sha256_1},
              "bar": {"sha256": self.sha256_1},
              "foobarbaz": {"sha256": self.sha256_1}
          },
          products={
              "baz" : {"sha256": self.sha256_1},
              "foo": {"sha256": self.sha256_1},
              "bar": {"sha256": self.sha256_2},
              "foobarbaz": {"sha256": self.sha256_1}

          }
      ))
    }

  def test_pass_rules_with_each_rule_type(self):
    """Pass with list of rules of each rule type. """
    rules = [
      ["DELETE", "foobar"],
      ["CREATE", "baz"],
      ["MODIFY", "bar"],
      ["MATCH", "foo", "WITH", "MATERIALS", "FROM", "item"], # match with self
      ["ALLOW", "foobarbaz"],
      ["DISALLOW", "*"],
    ]
    for source_type in ["materials", "products"]:
      verify_item_rules(self.item_name, source_type, rules, self.links)

  def test_fail_disallow_not_consumed_artifacts(self):
    """Fail with not consumed artifacts and terminal DISALLOW. """
    rules = [
      ["DISALLOW", "*"],
    ]
    with self.assertRaises(RuleVerificationError):
      verify_item_rules(self.item_name, "materials", rules, self.links)

  def test_fail_wrong_source_type(self):
    """Fail with wrong source_type."""
    with self.assertRaises(securesystemslib.exceptions.FormatError):
      verify_item_rules(self.item_name, "artifacts", [], self.links)

  def test_pass_not_consumed_artifacts(self):
    """Pass with not consumed artifacts and implicit terminal ALLOW * """
    verify_item_rules(self.item_name, "materials", [], self.links)


class TestVerifyAllItemRules(unittest.TestCase):
  """Test verifylib.verify_all_item_rules(items, links). """

  def setUp(self):
    """Create a dummy supply chain with two steps one inspection and the
    according link metadata:

    write-code (Step) ->  package (step) -> untar (Inspection)

    'write-code' creates an artifact foo
    'package' creates foo.tar.gz and deletes foo
    'untar' untars foo.tar.gz which results in foo.tar.gz and foo

    """

    self.sha256_foo = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"
    self.sha256_foo_tar = \
        "93c3c35a039a6a3d53e81c5dbee4ebb684de57b7c8be11b8739fd35804a0e918"

    self.steps = [
        Step(name="write-code",
            expected_products=[
                ["CREATE", "foo"]
            ],
        ),
        Step(name="package",
            expected_materials=[
                ["MATCH", "foo", "WITH", "PRODUCTS", "FROM", "write-code"]
            ],
            expected_products=[
                ["CREATE", "foo.tar.gz"],
                ["DELETE", "foo"]
            ],
        )
    ]

    self.inspections = [
        Inspection(name="untar",
            expected_materials=[
                ["MATCH", "foo.tar.gz", "WITH", "PRODUCTS", "FROM", "package"]
            ],
            expected_products=[
                ["MATCH", "foo", "IN", "dir", "WITH", "PRODUCTS",
                    "FROM", "write-code"]
            ]
        )
    ]

    self.links = {
      "write-code" : Metablock(signed=Link(name="write-code",
          products={
              "foo": {
                  "sha256": self.sha256_foo
              }
          }
      )),
      "package" : Metablock(signed=Link(name="package",
          materials={
              "foo": {
                  "sha256": self.sha256_foo
              }
          },
          products={
              "foo.tar.gz": {
                  "sha256": self.sha256_foo_tar
              }
          }
      )),
        "untar" : Metablock(signed=Link(name="untar",
            materials={
                "foo.tar.gz": {
                    "sha256": self.sha256_foo_tar
                }
            },
            products={
                "dir/foo": {
                    "sha256": self.sha256_foo
                },
            }
        ))
    }

  def test_pass_verify_all_step_rules(self):
    """Pass rule verification for dummy supply chain Steps. """
    verify_all_item_rules(self.steps, self.links)

  def test_pass_verify_all_inspection_rules(self):
    """Pass rule verification for dummy supply chain Inspections. """
    verify_all_item_rules(self.inspections, self.links)


class TestInTotoVerify(unittest.TestCase):
  """
  Tests verifylib.in_toto_verify(layout_path, layout_key_paths).

  Uses in-toto demo supply chain link metadata files and basic layout for
  verification.

  Copies the basic layout for different test scenarios:
    - single-signed layout
    - double-signed layout
    - expired layout
    - layout with failing link rule
    - layout with failing step rule

  """
  @classmethod
  def setUpClass(self):
    """Creates and changes into temporary directory.
    Copies demo files to temp dir...
      - owner/functionary key pairs
      - *.link metadata files
      - layout template (not signed, no expiration date)
      - final product

    ...and dumps various layouts for different test scenarios
    """
    # Backup original cwd
    self.working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # find where the scripts directory is located.
    scripts_directory = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "scripts")

    # Create and change into temporary directory
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)

    # Copy demo files to temp dir
    for file in os.listdir(demo_files):
      shutil.copy(os.path.join(demo_files, file), self.test_dir)

    # copy scripts over
    shutil.copytree(scripts_directory, "scripts")

    # Load layout template
    layout_template = Metablock.load("demo.layout.template")

    # Store various layout paths to be used in tests
    self.layout_single_signed_path = "single-signed.layout"
    self.layout_double_signed_path = "double-signed.layout"
    self.layout_bad_sig = "bad-sig.layout"
    self.layout_expired_path = "expired.layout"
    self.layout_failing_step_rule_path = "failing-step-rule.layout"
    self.layout_failing_inspection_rule_path = "failing-inspection-rule.layout"
    self.layout_failing_inspection_retval = "failing-inspection-retval.layout"
    self.layout_no_steps_no_inspections = "no_steps_no_inspections.layout"


    # Import layout signing keys
    alice = import_rsa_key_from_file("alice")
    bob = import_rsa_key_from_file("bob")
    self.alice_path = "alice.pub"
    self.bob_path = "bob.pub"

    # dump single signed layout
    layout = copy.deepcopy(layout_template)
    layout.sign(alice)
    layout.dump(self.layout_single_signed_path)

    # dump double signed layout
    layout = copy.deepcopy(layout_template)
    layout.sign(alice)
    layout.sign(bob)
    layout.dump(self.layout_double_signed_path)

    # dump layout with bad signature
    layout = copy.deepcopy(layout_template)
    layout.sign(alice)
    layout.signed.readme = "this breaks the signature"
    layout.dump(self.layout_bad_sig)

    # dump expired layout
    layout = copy.deepcopy(layout_template)
    layout.signed.expires = (datetime.today() +
        relativedelta(months=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    layout.sign(alice)
    layout.dump(self.layout_expired_path)

    # dump layout with failing step rule
    layout = copy.deepcopy(layout_template)
    layout.signed.steps[0].expected_products.insert(0,
        ["DISALLOW", "*"])
    layout.signed.steps[0].expected_products.insert(0,
        ["MODIFY", "*"])
    layout.sign(alice)
    layout.dump(self.layout_failing_step_rule_path)

    # dump layout with failing inspection rule
    layout = copy.deepcopy(layout_template)
    layout.signed.inspect[0].expected_materials.insert(0,
        ["MODIFY", "*"])
    layout.signed.inspect[0].expected_materials.append(
        ["DISALLOW", "*"])
    layout.sign(alice)
    layout.dump(self.layout_failing_inspection_rule_path)

    # dump layout with failing inspection retval
    layout = copy.deepcopy(layout_template)
    layout.signed.inspect[0].run = ["python", "./scripts/expr",  "1", "/", "0"]
    layout.sign(alice)
    layout.dump(self.layout_failing_inspection_retval)

    # dump empty layout
    layout = Metablock(signed=Layout())
    layout.sign(alice)
    layout.dump(self.layout_no_steps_no_inspections)
    self.alice = alice

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp dir. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_verify_passing(self):
    """Test pass verification of single-signed layout. """
    layout = Metablock.load(self.layout_single_signed_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    in_toto_verify(layout, layout_key_dict)

  def test_verify_passing_double_signed_layout(self):
    """Test pass verification of double-signed layout. """
    layout = Metablock.load(self.layout_double_signed_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path, self.bob_path])
    in_toto_verify(layout, layout_key_dict)

  def test_verify_passing_empty_layout(self):
    """Test pass verification of layout without steps or inspections. """
    layout = Metablock.load(self.layout_no_steps_no_inspections)
    layout_key_dict = import_public_keys_from_files_as_dict(
        [self.alice_path])
    in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_wrong_key(self):
    """Test fail verification with wrong layout key. """
    layout = Metablock.load(self.layout_single_signed_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.bob_path])
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_bad_signature(self):
    """Test fail verification with bad layout signature. """
    layout = Metablock.load(self.layout_bad_sig)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_layout_expired(self):
    """Test fail verification with expired layout. """
    layout = Metablock.load(self.layout_expired_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(LayoutExpiredError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_link_metadata_files(self):
    """Test fail verification with link metadata files not found. """
    os.rename("package.2f89b927.link", "package.link.bak")
    layout = Metablock.load(self.layout_single_signed_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(in_toto.exceptions.LinkNotFoundError):
      in_toto_verify(layout, layout_key_dict)
    os.rename("package.link.bak", "package.2f89b927.link")

  def test_verify_failing_inspection_exits_non_zero(self):
    """Test fail verification with inspection returning non-zero. """
    layout = Metablock.load(self.layout_failing_inspection_retval)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(BadReturnValueError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_step_rules(self):
    """Test fail verification with failing step artifact rule. """
    layout = Metablock.load(self.layout_failing_step_rule_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(RuleVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_failing_inspection_rules(self):
    """Test fail verification with failing inspection artifact rule. """
    layout = Metablock.load(self.layout_failing_inspection_rule_path)
    layout_key_dict = import_public_keys_from_files_as_dict([self.alice_path])
    with self.assertRaises(RuleVerificationError):
      in_toto_verify(layout, layout_key_dict)

  def test_verify_layout_signatures_fail_with_no_keys(self):
    """Layout signature verification fails when no keys are passed. """
    layout_metablock = Metablock(signed=Layout())
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout_metablock, {})

  def test_verify_layout_signatures_fail_with_malformed_signature(self):
    """Layout signature verification fails with malformed signatures. """
    layout_metablock = Metablock(signed=Layout())
    signature = layout_metablock.sign(self.alice)
    pubkey = self.alice
    pubkey["keyval"]["private"] = ""

    del signature["sig"]
    layout_metablock.signed.signatures = [signature]
    with self.assertRaises(SignatureVerificationError):
      in_toto_verify(layout_metablock, {self.alice["keyid"]: pubkey})





class TestInTotoVerifyThresholds(unittest.TestCase):
  """Test verifylib functions related to signature thresholds.

    - verifylib.verify_link_signature_thresholds
    - verifylib.verify_threshold_constraints """


  @classmethod
  def setUpClass(self):
    """Load test keys from demo files. """
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    self.alice = import_rsa_key_from_file(
        os.path.join(demo_files, "alice"))
    self.alice_pubkey = import_rsa_key_from_file(
        os.path.join(demo_files, "alice.pub"))
    self.alice_keyid = self.alice["keyid"]

    self.bob = import_rsa_key_from_file(
        os.path.join(demo_files, "bob"))
    self.bob_pubkey = import_rsa_key_from_file(
        os.path.join(demo_files, "bob.pub"))
    self.bob_keyid = self.bob["keyid"]

    self.name = "test"
    self.foo_hash = \
        "d65165279105ca6773180500688df4bdc69a2c7b771752f0a46ef120b7fd8ec3"


  def test_thresholds_skip_unauthorized_links(self):
    """Ignore links with unauthorized signatures. """
    # Layout with one step, one authorized functionary and threshold 1
    layout = Layout(
        keys={
          self.bob_keyid: self.bob_pubkey
        },
        steps=[
          Step(
            name=self.name,
            pubkeys=[self.bob_keyid])
          ]
      )

    # Signed links (one authorized the other one not)
    link_bob = Metablock(signed=Link(name=self.name))
    link_bob.sign(self.bob)
    link_alice = Metablock(signed=Link(name=self.name))
    link_alice.sign(self.alice)

    # The dictionary of links per step passed to the verify function
    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice
      }
    }

    # The dictionary of links expected to be returned, not containing the
    # unauthorized link, but enough (threshold) authorized links
    expected_chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob
      }
    }
    # Verify signatures/thresholds
    returned_chain_link_dict = verify_link_signature_thresholds(
        layout, chain_link_dict)
    # Test that the returned dict is as expected
    self.assertDictEqual(returned_chain_link_dict, expected_chain_link_dict)


  def test_thresholds_skip_links_with_failing_signature(self):
    """Ignore links with failing signatures. """

    # Layout with one step, two authorized functionaries and threshold 1
    layout = Layout(
        keys={
          self.bob_keyid: self.bob_pubkey,
          self.alice_keyid: self.alice_pubkey,
        },
        steps=[
          Step(
            name=self.name,
            pubkeys=[self.bob_keyid, self.alice_keyid],
            threshold=1)
          ]
        )

    # Authorized links (one signed one not)
    link_bob = Metablock(signed=Link(name=self.name))
    link_bob.sign(self.bob)
    link_alice = Metablock(signed=Link(name=self.name))

    # The dictionary of links per step passed to the verify function
    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice
      }
    }

    # The dictionary of links expected to be returned, not containing the
    # unauthorized link, but enough (threshold) authorized links
    expected_chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob
      }
    }

    # Verify signatures/thresholds
    returned_chain_link_dict = verify_link_signature_thresholds(
        layout, chain_link_dict)
    # Test that the returned dict is as expected
    self.assertDictEqual(returned_chain_link_dict, expected_chain_link_dict)


  def test_thresholds_fail_with_not_enough_valid_links(self):
    """ Fail with not enough authorized links. """

    # Layout with one step, two authorized functionaries and threshold 2
    layout = Layout(
        keys={
          self.bob_keyid: self.bob_pubkey,
          self.alice_keyid: self.alice_pubkey,
        },
        steps=[
          Step(
            name=self.name,
            pubkeys=[self.bob_keyid, self.alice_keyid],
            threshold=2)
          ]
        )

    # Only one authorized and validly signed link
    link_bob = Metablock(signed=Link(name=self.name))
    link_bob.sign(self.bob)

    # The dictionary of links per step passed to the verify function
    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob
      }
    }

    # Fail signature threshold verification with not enough links
    with self.assertRaises(ThresholdVerificationError):
      verify_link_signature_thresholds(layout, chain_link_dict)


  def test_threshold_constraints_fail_with_not_enough_links(self):
    """ Fail with not enough links. """
    # Layout with one step and threshold 2
    layout = Layout(steps=[Step(name=self.name, threshold=2)])
    # Authorized (unsigned) link
    # This function does not care for signatures it just verifies if the
    # different links have recorded the same artifacts. Signature verification
    # happens earlier in the final product verification (see tests above)
    link_bob = Metablock(signed=Link(name=self.name))

    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
      }
    }

    with self.assertRaises(ThresholdVerificationError):
      verify_threshold_constraints(layout, chain_link_dict)


  def test_threshold_constraints_fail_with_unequal_links(self):
    """ Test that the links for a step recorded the same artifacts. """
    # Layout with one step and threshold 2
    layout = Layout(steps=[Step(name=self.name, threshold=2)])
    link_bob = Metablock(
        signed=Link(
          name=self.name,
          materials={
            "foo": { "sha256": self.foo_hash}
          }
        )
      )
    # Cf. signing comment in test_thresholds_constraints_with_not_enough_links
    link_alice = Metablock(signed=Link(name=self.name))

    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice,
      }
    }

    with self.assertRaises(ThresholdVerificationError):
      verify_threshold_constraints(layout, chain_link_dict)



  def test_threshold_constraints_pas_with_equal_links(self):
    """ Pass threshold constraint verification with equal links. """
    # Layout with one step and threshold 2
    layout = Layout(steps=[Step(name=self.name, threshold=2)])
    # Two authorized links with equal artifact recordings (materials)
    # Cf. signing comment in test_thresholds_constraints_with_not_enough_links
    link_bob = Metablock(
        signed=Link(
          name=self.name,
          materials={
            "foo": { "sha256": self.foo_hash}
          }
        )
      )
    link_alice = Metablock(
        signed=Link(
          name=self.name,
          materials={
            "foo": { "sha256": self.foo_hash}
          }
        )
      )

    chain_link_dict = {
      self.name: {
        self.bob_keyid: link_bob,
        self.alice_keyid: link_alice,
      }
    }

    verify_threshold_constraints(layout, chain_link_dict)



@unittest.skipIf(os.getenv("TEST_SKIP_GPG"), "gpg not found")
class TestInTotoVerifyThresholdsGpgSubkeys(unittest.TestCase):
  """
  Test the following 8 scenarios for combinations of link authorization,
  where a link is either signed by a master or subkey (SIG), and the
  corresponding step authorizes either the master or subkey (AUTH), and the
  corresponding top level key in the layout key store is either a master key
  (bundle, i.e. with subkeys) or a subkey (KEY).

  M ... Masterkey
  S ... Subkey

  SIG AUTH KEY(bundle)| OK  | Comment
  ---------------------------------------------------------------
  M   M    M          | Yes | Normal scenario (*)
  M   M    S          | No  | Cannot find key in key store + cannot sign (*)
  M   S    M          | No  | Unallowed trust delegation + cannot sign (*)
  M   S    S          | No  | Unallowed trust delegation + cannot sign (*)
  S   M    M          | Yes | Allowed trust delegation
  S   M    S          | No  | Cannot associate keys
  S   S    M          | Yes | Can find key in key store
  S   S    S          | Yes | Generalizes to normal scenario

  (*) NOTE: Master keys with a subkey with signing capability always use that
  subkey, even if the master keyid is specified and has signing capability.


  Plus additional gpg subkey related threshold tests.

  """

  @classmethod
  def setUpClass(self):
    # Create directory to run the tests without having everything blow up
    self.working_dir = os.getcwd()

    # Find demo files
    gpg_keyring_path = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "gpg_keyrings", "rsa")

    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    self.gnupg_home = os.path.join(self.test_dir, "rsa")
    shutil.copytree(gpg_keyring_path, self.gnupg_home)

    self.master = "8465a1e2e0fb2b40adb2478e18fb3f537e0c8a17"
    self.sub = "c5a0abe6ec19d0d65f85e2c39be9df5131d924e9"

    master_key = in_toto.gpg.functions.gpg_export_pubkey(
        self.master, self.gnupg_home)
    sub_key = master_key["subkeys"][self.sub]

    # We need a gpg key without subkeys to test the normal scenario (M M M),
    # because keys with signing subkeys always use that subkey for signing.
    self.master2 = "7B3ABB26B97B655AB9296BD15B0BD02E1C768C43"
    master_key2 = in_toto.gpg.functions.gpg_export_pubkey(
        self.master2, self.gnupg_home)


    self.pub_key_dict = {
      self.master: master_key,
      self.sub: sub_key,
      self.master2: master_key2
    }

    self.step_name = "name"

    os.chdir(self.test_dir)


  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp test directory. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def _verify_link_signature_tresholds(self, sig_id, auth_id, key_id):
    metablock = Metablock(signed=Link(name=self.step_name))
    metablock.sign_gpg(sig_id, self.gnupg_home)                        # SIG

    chain_link_dict = {
      self.step_name : {
        sig_id : metablock                                             # SIG
      }
    }

    layout = Layout(
      steps=[
        Step(
            name=self.step_name,
            pubkeys=[auth_id]                                          # AUTH
          )
        ],
      keys={
          key_id: self.pub_key_dict[key_id]                            # KEY
        }
      )
    return layout, chain_link_dict


  def test_verify_link_signature_thresholds__M_M_M(self):
    """Normal scenario. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.master2, self.master2, self.master2)

    #print("path: {}".format(os.environ['PATH']))
    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__M_M_S__M_S_M__M_S_S(self):
    """Cannot sign with master key if subkey is present. """
    # The scenarios MMS, MSM, MSS are impossible because we cannot sign
    # with a master key, if there is a subkey with signing capability
    # GPG will always use that subkey.
    # Even if gpg would use the masterkey, these scenarios are not allowed,
    # see table in docstring of testcase
    signature = in_toto.gpg.functions.gpg_sign_object(
        b"data", self.master, self.gnupg_home)

    self.assertTrue(signature["keyid"] == self.sub)


  def test_verify_link_signature_thresholds__S_M_M(self):
    """Allowed trust delegation. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.master, self.master)
    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__S_M_S(self):
    """Cannot associate keys. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.master, self.sub)
    with self.assertRaises(ThresholdVerificationError):
      verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__S_S_M(self):
    """No trust delegation and can find key in key store. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.sub, self.master)
    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_link_signature_thresholds__S_S_S(self):
    """Generalizes to normal scenario. """
    layout, chain_link_dict = self._verify_link_signature_tresholds(
        self.sub, self.sub, self.sub)
    verify_link_signature_thresholds(layout, chain_link_dict)


  def test_verify_subkey_thresholds(self):
    """Subkeys of same main key count only once towards threshold. """

    masterkey = "40e692c3ae03f6b88dff95d0d2c9fe930766998d"
    subkey1 = "35830aa342b9fea0178876b02b25647ff0ef59fe"
    subkey2 = "732d722578f71a9ec967a64bfead922c91eb7351"

    link1 = Metablock(signed=Link(name=self.step_name))
    link1.sign_gpg(subkey1, self.gnupg_home)
    link2 = Metablock(signed=Link(name=self.step_name))
    link2.sign_gpg(subkey2, self.gnupg_home)

    chain_link_dict = {
      self.step_name : {
        subkey1: link1,
        subkey2: link2
      }
    }

    layout = Layout(
      steps=[
          Step(name=self.step_name, pubkeys=[masterkey], threshold=2)
        ],
      keys={
          masterkey: in_toto.gpg.functions.gpg_export_pubkey(
              masterkey, self.gnupg_home)
        }
      )
    with self.assertRaises(ThresholdVerificationError):
      verify_link_signature_thresholds(layout, chain_link_dict)



class TestVerifySublayouts(unittest.TestCase):
  """Tests verifylib.verify_sublayouts(layout, reduced_chain_link_dict).
  Call with one-step super layout that has a sublayout (demo layout). """

  @classmethod
  def setUpClass(self):
    """Creates and changes into temporary directory and prepares two layouts.
    The superlayout, which has one step and its sublayout, which is the usual
    demo layout (write code, package, inspect tar). """

    # Backup original cwd
    self.working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # find where the scripts directory is located.
    scripts_directory = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "scripts")

    # Create and change into temporary directory
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)

    # Copy demo files to temp dir
    for file in os.listdir(demo_files):
      shutil.copy(os.path.join(demo_files, file), self.test_dir)

    # copy portable scripts over
    shutil.copytree(scripts_directory, 'scripts')

    # Import sub layout signing (private) and verifying (public) keys
    alice = import_rsa_key_from_file("alice")
    alice_pub = import_rsa_key_from_file("alice.pub")

    # From the perspective of the superlayout, the sublayout is treated as
    # a link corresponding to a step, hence needs a name.
    sub_layout_name = "sub_layout"

    # Sublayout links are expected in a directory relative to the superlayout's
    # link directory
    sub_layout_link_dir = SUBLAYOUT_LINK_DIR_FORMAT.format(
        name=sub_layout_name, keyid=alice["keyid"])

    for sublayout_link_name in glob.glob("*.link"):
      dest_path = os.path.join(sub_layout_link_dir, sublayout_link_name)
      os.renames(sublayout_link_name, dest_path)


    # Copy, sign and dump sub layout as link from template
    layout_template = Metablock.load("demo.layout.template")
    sub_layout = copy.deepcopy(layout_template)
    sub_layout_path = FILENAME_FORMAT.format(step_name=sub_layout_name,
        keyid=alice_pub["keyid"])
    sub_layout.sign(alice)
    sub_layout.dump(sub_layout_path)

    # Create super layout that has only one step, the sublayout
    self.super_layout = Layout()
    self.super_layout.keys[alice_pub["keyid"]] = alice_pub
    sub_layout_step = Step(
        name=sub_layout_name,
        pubkeys=[alice_pub["keyid"]]
      )
    self.super_layout.steps.append(sub_layout_step)

    # Load the super layout links (i.e. the sublayout)
    self.super_layout_links = load_links_for_layout(self.super_layout, ".")

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp dir. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_verify_demo_as_sublayout(self):
    """Test super layout's passing sublayout verification. """
    verify_sublayouts(
        self.super_layout, self.super_layout_links, ".")





class TestInTotoVerifyMultiLevelSublayouts(unittest.TestCase):
  """Test verifylib.in_toto_verify with multiple levels of sublayouts. """

  def test_verify_multi_level_sublayout(self):
    # Backup original cwd
    working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # Create and change into temporary directory
    test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(test_dir)

    # We don't need to copy the demo files, we just load the keys
    keys = {}
    for key_name in ["alice", "bob", "carl"]:
      keys[key_name + "_priv"] = import_rsa_key_from_file(
          os.path.join(demo_files, key_name))
      keys[key_name + "_pub"] = import_rsa_key_from_file(
          os.path.join(demo_files, key_name + ".pub"))


    # Create layout hierarchy

    # Root layout
    # The root layout is the layout that will be passed to `in_toto_verify`
    # It only has one step which is a sublayout, into which verification
    # recurses. Only the root layout and root layout verification key will be
    # passed to verification.
    root_layout_pub_key_dict = {
        keys["alice_pub"]["keyid"]: keys["alice_pub"]
      }

    root_layout_name = "root.layout"
    root_layout_step_name = "delegated-to-bob"

    root_layout = Metablock(signed=Layout(
        keys={
          keys["bob_pub"]["keyid"]: keys["bob_pub"]
        },
        steps=[
            Step(
              name=root_layout_step_name,
              pubkeys=[
                keys["bob_pub"]["keyid"]
              ]
            )
          ]
        )
      )
    root_layout.sign(keys["alice_priv"])


    # Sublayout (first level)
    # The first level sublayout wil be treated as a link from the
    # superlayout's perspective and loaded from the current working directory.
    # The link for the only step of this sublayout will be placed in a
    # namespaced subdir, that link itself is a sublayout (subsublayout).
    bobs_layout_name = FILENAME_FORMAT.format(
        step_name=root_layout_step_name,
        keyid=keys["bob_pub"]["keyid"])

    bobs_layout_link_dir = SUBLAYOUT_LINK_DIR_FORMAT.format(
        name=root_layout_step_name,
        keyid=keys["bob_pub"]["keyid"])
    os.mkdir(bobs_layout_link_dir)

    bobs_layout_step_name = "delegated-to-carl"

    bobs_layout = Metablock(signed=Layout(
        keys={
          keys["carl_pub"]["keyid"]: keys["carl_pub"]
          },
        steps=[
            Step(
              name=bobs_layout_step_name,
              pubkeys=[keys["carl_pub"]["keyid"]]
            )
          ]
        )
      )
    bobs_layout.sign(keys["bob_priv"])
    bobs_layout.dump(bobs_layout_name)


    # Subsublayout (second level)
    # The subsublayout will be placed in the namespaced link dir
    # of its superlayout (sublayout from the root layout's perspective), for
    # for which it serves as link.
    carls_layout_name = FILENAME_FORMAT.format(
            step_name=bobs_layout_step_name,
            keyid=keys["carl_pub"]["keyid"])

    carls_layout_path = os.path.join(bobs_layout_link_dir, carls_layout_name)
    carls_layout = Metablock(signed=Layout())
    carls_layout.sign(keys["carl_priv"])
    carls_layout.dump(carls_layout_path)

    in_toto_verify(root_layout, root_layout_pub_key_dict)

    os.chdir(working_dir)
    shutil.rmtree(test_dir)


class TestSublayoutVerificationMatchRule(unittest.TestCase):
  """Tests a sublayout and checks if a MATCH rule is successful after sublayout
  is resolved into a summary link."""

  def test_verify_sublayout_match_rule(self):

    # Backup original cwd
    working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
      os.path.dirname(os.path.realpath(__file__)), "demo_files")

    script_files = os.path.join(
      os.path.dirname(os.path.realpath(__file__)), "scripts")

    # Create and change into temporary directory
    test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(test_dir)

    # We don't need to copy the demo files, we just load the keys
    keys = {}
    for key_name in ["alice", "bob"]:
      keys[key_name + "_priv"] = import_rsa_key_from_file(
        os.path.join(demo_files, key_name))
      keys[key_name + "_pub"] = import_rsa_key_from_file(
        os.path.join(demo_files, key_name + ".pub"))

    # Create layout hierarchy

    # Root layout
    # The root layout is the layout that will be passed to `in_toto_verify`
    # It only has one step which is a sublayout, into which verification
    # recurses. Only the root layout and root layout verification key will be
    # passed to verification.
    root_layout_pub_key_dict = {
      keys["alice_pub"]["keyid"]: keys["alice_pub"]
    }

    root_layout_step_name = "delegated-to-bob"

    root_layout = Metablock(signed=Layout(
      keys={
        keys["bob_pub"]["keyid"]: keys["bob_pub"]
      },
      steps=[
        Step(
          name=root_layout_step_name,
          pubkeys=[
            keys["bob_pub"]["keyid"]
          ],
          expected_products=[["MATCH", "foo.tar.gz", "WITH", "PRODUCTS",
              "FROM", root_layout_step_name], ["DISALLOW", "*"]]
        )
      ]
    )
    )
    root_layout.sign(keys["alice_priv"])


    # Sublayout (first level)
    # The sublayout will be treated as a link from the superlayout's
    # perspective and loaded from the current working directory. The links for
    # the steps of this sublayout will be placed in a namespaced subdir.
    bobs_layout_name = FILENAME_FORMAT.format(
      step_name=root_layout_step_name,
      keyid=keys["bob_pub"]["keyid"])

    bobs_layout_link_dir = SUBLAYOUT_LINK_DIR_FORMAT.format(
      name=root_layout_step_name,
      keyid=keys["bob_pub"]["keyid"])
    os.mkdir(bobs_layout_link_dir)

    bobs_layout = Metablock.load(os.path.join(demo_files, "demo.layout.template"))
    bobs_layout.sign(keys["bob_priv"])
    bobs_layout.dump(bobs_layout_name)
    shutil.copy2(os.path.join(demo_files, "write-code.776a00e2.link"), bobs_layout_link_dir)
    shutil.copy2(os.path.join(demo_files, "package.2f89b927.link"), bobs_layout_link_dir)
    shutil.copy2(os.path.join(demo_files, "foo.tar.gz"), ".")
    shutil.copytree(script_files, os.path.join(".", "scripts"))

    in_toto_verify(root_layout, root_layout_pub_key_dict)

    os.chdir(working_dir)
    shutil.rmtree(test_dir)



class TestGetSummaryLink(unittest.TestCase):
  """Tests verifylib.get_summary_link(layout, reduced_chain_link_dict).
  Pass two step demo layout and according link files and verify the
  returned summary link.
  """

  @classmethod
  def setUpClass(self):
    """Creates and changes into temporary directory and prepares two layouts.
    The superlayout, which has one step and its sublayout, which is the usual
    demo layout (write code, package, inspect tar). """

    # Backup original cwd
    self.working_dir = os.getcwd()

    # Find demo files
    demo_files = os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "demo_files")

    # Create and change into temporary directory
    self.test_dir = os.path.realpath(tempfile.mkdtemp())
    os.chdir(self.test_dir)

    # Copy demo files to temp dir
    for file in os.listdir(demo_files):
      shutil.copy(os.path.join(demo_files, file), self.test_dir)

    self.demo_layout = Metablock.load("demo.layout.template")
    self.code_link = Metablock.load("package.2f89b927.link")
    self.package_link = Metablock.load("write-code.776a00e2.link")
    self.demo_links = {
        "write-code": self.code_link,
        "package": self.package_link
      }

  @classmethod
  def tearDownClass(self):
    """Change back to initial working dir and remove temp dir. """
    os.chdir(self.working_dir)
    shutil.rmtree(self.test_dir)

  def test_get_summary_link_from_demo_layout(self):
    """Create summary link from demo link files and compare properties. """
    sum_link = get_summary_link(self.demo_layout.signed, self.demo_links, "")

    self.assertEquals(sum_link.signed._type, self.code_link.signed._type)
    self.assertEquals(sum_link.signed.name, "")
    self.assertEquals(sum_link.signed.materials, self.code_link.signed.materials)

    self.assertEquals(sum_link.signed.products, self.package_link.signed.products)
    self.assertEquals(sum_link.signed.command, self.package_link.signed.command)
    self.assertEquals(sum_link.signed.byproducts, self.package_link.signed.byproducts)
    self.assertEquals(sum_link.signed.byproducts.get("return-value"),
        self.package_link.signed.byproducts.get("return-value"))



if __name__ == "__main__":
  unittest.main()
