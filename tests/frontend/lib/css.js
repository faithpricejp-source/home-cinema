"use strict";
/* 极简 CSS 规则解析（只为审计用）：保留每条规则所在的 @media 条件。 */
const fs = require("fs");
const path = require("path");

const CSS_PATH = path.resolve(__dirname, "../../../homecinema/web/app.css");
const HTML_PATH = path.resolve(__dirname, "../../../homecinema/web/index.html");

function rules(src) {
  const out = [];
  const noComments = src.replace(/\/\*[\s\S]*?\*\//g, "");
  let buf = "", media = "", depth = 0, cur = null;
  for (let i = 0; i < noComments.length; i++) {
    const ch = noComments[i];
    if (ch === "{") {
      const head = buf.trim().replace(/\s+/g, " ");
      if (/^@/.test(head)) { if (depth === 0) media = head + " => "; }
      else { cur = { sel: head, decl: "", media }; out.push(cur); }
      buf = ""; depth++; continue;
    }
    if (ch === "}") {
      depth--;
      if (cur && !cur.decl) { cur.decl = buf.trim(); cur = null; }
      if (depth === 0) media = "";
      buf = ""; continue;
    }
    buf += ch;
  }
  return out.filter((r) => r.decl);
}

function readCss() { return fs.readFileSync(CSS_PATH, "utf8"); }
function readHtml() { return fs.readFileSync(HTML_PATH, "utf8"); }

module.exports = { rules, readCss, readHtml, CSS_PATH, HTML_PATH };
