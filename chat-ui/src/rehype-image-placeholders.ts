type Node = {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  value?: string;
  children?: Node[];
};

// A model may repeat an already displayed image as ![description](). This is
// not a failed download: it has no resource. Drop only empty image references
// before the normal sanitizer/hardener, preserving valid and blocked URLs.
export function rehypeImagePlaceholders() {
  return (tree: Node) => {
    function clean(node: Node): boolean {
      if (node.type === "element" && node.tagName === "img" &&
          (typeof node.properties?.src !== "string" || !node.properties.src.trim())) {
        return true;
      }
      let removed = false;
      if (node.children) {
        node.children = node.children.filter(child => {
          if (!clean(child)) return true;
          removed = true;
          return false;
        });
      }
      // Remove a now-empty paragraph, including an orphan comma after the image.
      return removed && node.type === "element" && node.tagName === "p" &&
        (node.children || []).every(child => child.type === "text" && /^[\s,，]*$/.test(child.value || ""));
    }
    clean(tree);
  };
}
