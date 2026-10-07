import unittest

from code_agent.analysis import analyze_source


class AnalysisTests(unittest.TestCase):
    def rules(self, source):
        return {f["rule"] for f in analyze_source(source, "demo.py")["findings"]}

    def test_mutable_positional_and_keyword_defaults(self):
        result = analyze_source("def f(a=[], *, b={}):\n    pass\n", "demo.py")
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["findings"][0]["line"], 1)
        self.assertEqual(result["functions"][0]["name"], "f")

    def test_safe_defaults_and_identity(self):
        self.assertEqual(self.rules("def f(a=None):\n    return a is None\nx = True\nassert x is True"), set())

    def test_aliases_and_shell(self):
        source = "import subprocess as sp\nfrom pickle import loads as unpickle\nsp.run(['echo', 'x'], shell=True)\nunpickle(data)"
        self.assertEqual(self.rules(source), {"shell-execution", "unsafe-deserialization"})

    def test_shell_false_is_not_reported(self):
        self.assertNotIn("shell-execution", self.rules("import subprocess\nsubprocess.run(['echo'], shell=False)"))

    def test_bare_swallowed_exceptions(self):
        source = "try:\n    int('x')\nexcept:\n    pass"
        self.assertEqual(self.rules(source), {"bare-except", "swallowed-exception"})

    def test_specific_handler_not_reported_as_swallowed(self):
        self.assertEqual(self.rules("try:\n    int('x')\nexcept ValueError:\n    pass"), set())

    def test_dynamic_execution_and_zero(self):
        self.assertEqual(self.rules("eval(text)\nexec(text)\nx = 1 / 0"), {"dynamic-execution", "zero-division"})

    def test_value_identity(self):
        self.assertEqual(self.rules("if x is 'ok':\n    pass"), {"literal-identity"})

    def test_syntax_error_is_structured(self):
        result = analyze_source("def broken(\n", "broken.py")
        self.assertFalse(result["syntax_ok"])
        self.assertEqual(result["findings"][0]["path"], "broken.py")
        self.assertEqual(result["findings"][0]["rule"], "syntax-error")
