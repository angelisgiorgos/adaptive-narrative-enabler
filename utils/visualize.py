
import networkx as nx
import matplotlib.pyplot as plt
# ============================
# VISUALIZATION
# ============================
def visualize_world(state):
    G = nx.DiGraph()

    for loc in state.world.locations.values():
        G.add_node(loc.name)

    for loc in state.world.locations.values():
        for conn in loc.connected:
            G.add_edge(loc.name, conn)

    pos = nx.spring_layout(G, seed=42)

    plt.figure(figsize=(8, 6))

    nx.draw(G, pos, with_labels=True)

    if state.visited_edges:
        nx.draw_networkx_edges(
            G, pos,
            edgelist=state.visited_edges,
            edge_color="red",
            width=2
        )

    plt.title("Story Graph")
    plt.show()
