def has_cycle(graph):
    visited, active = set(), set()

    def visit(node):
        if node in active:
            return False
        if node in visited:
            return False
        active.add(node)
        for child in graph.get(node, []):
            if visit(child):
                return True
        active.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)
