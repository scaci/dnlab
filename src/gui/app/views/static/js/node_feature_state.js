/** Authoritative node-feature state resolver used by the Properties panel. */
const NodeFeatureState = (() => {
  async function resolve(api, labId, nodeData) {
    if (!api || !api.Labs || !labId || !nodeData) return nodeData;
    const topology = await api.Labs.getTopology(labId);
    const nodeId = nodeData.id || nodeData.name;
    const authoritativeNode = (topology.nodes || []).find(n => n.name === nodeId);
    if (!authoritativeNode) return nodeData;
    const sidecar = topology.gui_node_features_state || {};
    return {
      ...nodeData,
      ...authoritativeNode,
      id: nodeId,
      node_features_state: sidecar[nodeId] || null,
    };
  }

  return { resolve };
})();

if (typeof module !== 'undefined' && module.exports) {
  module.exports = NodeFeatureState;
}
