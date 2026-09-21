"""Run against a built C++ solver; no external SMT solver is needed."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOLVER = os.environ.get(
    "NEUROSYM_CPP_SOLVER",
    str(Path(__file__).resolve().parents[1] / "neurosym_cpp/bitblast_solver"),
)


class ModelCompletionTests(unittest.TestCase):
    def solve(self, text, *flags):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "case.smt2"
            path.write_text(text)
            return subprocess.run(
                [SOLVER, str(path), *flags], text=True, capture_output=True,
                check=True, timeout=10,
            ).stdout

    def test_unused_boolean_completed_without_overwriting_constraints(self):
        formula = """
        (declare-fun spare () Bool)
        (declare-fun required () Bool)
        (assert required)
        (check-sat)
        """
        text = self.solve(formula, "--model-sorts")
        self.assertIn("spare = false : Bool", text)
        self.assertIn("required = true : Bool", text)
        native = self.solve(formula, "--esbmc-model")
        self.assertIn("(define-fun spare () Bool false)", native)
        self.assertIn("(define-fun required () Bool true)", native)

    def test_substituted_boolean_value_is_preserved(self):
        text = self.solve("""
        (declare-fun x () Bool)
        (declare-fun y () Bool)
        (assert (= y (not x)))
        (assert (not x))
        (check-sat)
        """, "--esbmc-model")
        self.assertIn("(define-fun x () Bool false)", text)
        self.assertIn("(define-fun y () Bool true)", text)

    def test_unsat_does_not_emit_model(self):
        text = self.solve("""
        (declare-fun spare () Bool)
        (declare-fun x () Bool)
        (assert x)
        (assert (not x))
        (check-sat)
        """, "--esbmc-model")
        self.assertEqual(text.strip(), "unsat")

    def test_bitvector_output_and_unused_bitvector(self):
        text = self.solve("""
        (declare-fun x () (_ BitVec 8))
        (declare-fun unused () (_ BitVec 8))
        (assert (= x #x2a))
        (check-sat)
        """, "--esbmc-model")
        self.assertIn("(define-fun x () (_ BitVec 8) 42)", text)
        self.assertNotIn("unreferenced", text)
        self.assertNotIn("define-fun unused", text)


if __name__ == "__main__":
    unittest.main()
