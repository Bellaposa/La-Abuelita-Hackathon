// Renders dashboard/index.html's drawLearning() against a minimal fake DOM and prints the visible text as JSON.
// Usage: node js_learning_check.js <index.html> <state.json>
const fs = require("fs");
const html = fs.readFileSync(process.argv[2], "utf8").split(String.fromCharCode(13)).join("");   // the page may have CRLF endings
const state = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));

class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this._text = ""; this.className = ""; }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...cs) { this.children = cs; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(" "); }
}
const registry = {};
const document = {
  getElementById: (id) => (registry[id] = registry[id] || new Node("div")),
  createElement: (t) => new Node(t),
  createElementNS: (ns, t) => new Node(t),
  createTextNode: (s) => { const n = new Node("#text"); n._text = String(s); return n; },
};

// pull the helper functions straight out of the page so the test runs the real code
const NL = String.fromCharCode(10);
const pick = (decl) => {
  const tail = decl.startsWith("function") ? "(" : " ";     // "function el(" / "const fmt ="
  let i = html.indexOf(NL + decl + tail);
  i = i >= 0 ? i + 1 : (html.startsWith(decl + tail) ? 0 : -1);
  if (i < 0) throw new Error("cannot find " + decl + " in index.html");
  if (decl.startsWith("function")) {                        // brace matching: bodies contain semicolons and nested blocks
    let depth = 0, k = html.indexOf("{", i);
    for (; k < html.length; k++) { if (html[k] === "{") depth++; else if (html[k] === "}" && --depth === 0) break; }
    return html.slice(i, k + 1);
  }
  return html.slice(i, html.indexOf(NL, i));                // a one-line const
};
const src = ["const $ = (id) => document.getElementById(id);", pick("function el"), pick("const fmt"), pick("const replace"),
             pick("function table"), pick("function drawLearning")].join(NL);
const fn = new Function("document", src + NL + "drawLearning(" + JSON.stringify(state) + "); return document.getElementById('learning');");
const out = fn(document);
console.log(JSON.stringify({ text: out.textContent, blocks: out.children.length }));
