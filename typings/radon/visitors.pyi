import ast
from typing import Self

class Function:
    name: str
    lineno: int
    col_offset: int
    endline: int
    is_method: bool
    classname: str | None
    complexity: int
    closures: list[Function]

class Class:
    name: str
    lineno: int
    col_offset: int
    endline: int
    methods: list[Function]
    inner_classes: list[Class]
    real_complexity: int
    @property
    def complexity(self) -> int: ...

class ComplexityVisitor(ast.NodeVisitor):
    functions: list[Function]
    classes: list[Class]
    @classmethod
    def from_ast(cls, ast_node: ast.AST, **kwargs: object) -> Self: ...
    @property
    def blocks(self) -> list[Function | Class]: ...
