"""Template checks that cfn-lint does not cover: CloudFormation rejects null values anywhere,
including inside an inline OpenAPI body, and a stray comma in a YAML flow mapping creates one."""
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


class Loader(yaml.SafeLoader):
    pass


def _tag(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_mapping(node, deep=True)


Loader.add_multi_constructor("!", _tag)


def nulls(node, path=()):
    if node is None:
        yield "/".join(path)
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from nulls(value, path + (str(key),))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from nulls(value, path + (str(index),))


class TemplateTests(unittest.TestCase):
    def test_no_null_values(self):
        for name in ("template.yaml", "pipeline/cicd.yaml", "governance/governance.yaml"):
            doc = yaml.load((ROOT / name).read_text(), Loader=Loader)
            self.assertEqual(list(nulls(doc)), [], name)


if __name__ == "__main__":
    unittest.main()
