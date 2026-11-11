"""Compute independent references for supported task contracts."""

import ast
import csv
import io
import operator
import re

from contest_harness.workspace.paths import PATH_PATTERN, safe_path

DATA_LIMIT = 4 * 1024 * 1024
BINARY_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod}


def integer_expression(node):
    if isinstance(node, ast.Constant) and type(node.value) is int:
        value = node.value
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = integer_expression(node.operand)
        if isinstance(node.op, ast.USub):
            value = -value
    elif isinstance(node, ast.BinOp) and type(node.op) in BINARY_OPS:
        value = BINARY_OPS[type(node.op)](integer_expression(node.left), integer_expression(node.right))
    else:
        raise ValueError("Unsupported expression")
    if value.bit_length() > 256:
        raise ValueError("Expression exceeds the supported range")
    return value


def bounded_text(path):
    if not path.is_file():
        raise ValueError("Verification requires a regular input file")
    with path.open("rb") as source:
        data = source.read(DATA_LIMIT + 1)
    if len(data) > DATA_LIMIT:
        raise ValueError("Input exceeds the verification limit")
    return data.decode("utf-8")


def csv_reference(problem, workspace):
    table = re.fullmatch(
        rf'Sum (?:the )?(\w+)(?: column)? in `?({PATH_PATTERN}\.csv)`?'
        r'(?: where (\w+)\s*=\s*"([^"\r\n]*)"'
        r'(?: and (\w+) is between (-?\d+) and (-?\d+) inclusive)?)?',
        problem, re.I,
    )
    if table is None:
        return None
    column, name, category, value, range_column, low, high = table.groups()
    if range_column and int(low) > int(high):
        return None
    source = bounded_text(safe_path(workspace, name))
    reader = csv.DictReader(io.StringIO(source), strict=True)
    headers = reader.fieldnames or []
    required = {field for field in (column, category, range_column) if field}
    if len(set(headers)) != len(headers) or not required.issubset(headers):
        return None
    total = 0
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            return None
        if category and row[category] != value:
            continue
        if range_column and not int(low) <= int(row[range_column]) <= int(high):
            continue
        total += int(row[column])
    return str(total), "csv_filtered_sum" if category else "csv_sum"


def compute_reference(instruction, workspace, kind):
    """Recognize only complete, independently executable instructions."""
    problem = re.sub(
        r"(?:\s+Work only inside the current directory\.|"
        r"(?:\s+|,?\s+and\s+)(?:write|submit|return|output) (?:only )?(?:the )?"
        r"(?:final )?(?:integer|answer|value)(?: to answer\.txt(?: in the current directory)?)?\.?)+\s*$",
        "", instruction.strip(), flags=re.I,
    ).rstrip(".?")
    try:
        if kind == "integer":
            expression = re.fullmatch(r"(?:compute|calculate|evaluate|return the integer)\s+([\d()+*/% \t-]{1,160})",
                                      problem, re.I | re.ASCII)
            if expression:
                return str(integer_expression(ast.parse(expression[1], mode="eval").body)), "integer_expression"
            return csv_reference(problem, workspace)
        if kind == "line":
            field = re.fullmatch(
                rf'(?:What is|Find|Read|Return) (?:the )?(?:value of )?"([^"\r\n:]+)" (?:in|from) `?({PATH_PATTERN})`?',
                problem, re.I,
            )
            if field:
                lines = bounded_text(safe_path(workspace, field[2])).splitlines()
                pattern = re.compile(rf"^{re.escape(field[1])}:[ \t]*(.+?)\s*$")
                values = [match[1] for line in lines if (match := pattern.fullmatch(line))]
                if len(values) == 1:
                    return values[0], "labelled_field"
    except (OSError, ValueError, RecursionError, ArithmeticError, csv.Error):
        pass
    return None
