from __future__ import annotations


UI_SCAN_SCRIPT = r"""
() => {
  const MAX_RESULTS = 240;
  const MAX_WORK_ITEMS = 520;
  const pageWidth = Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0, window.innerWidth);
  const pageHeight = Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0, window.innerHeight);

  const nativeInteractiveTags = new Set(["BUTTON", "A", "INPUT", "SELECT", "TEXTAREA", "OPTION", "SUMMARY"]);
  const roleInteractive = new Set([
    "button",
    "link",
    "menuitem",
    "tab",
    "option",
    "treeitem",
    "listitem",
    "row",
    "cell",
    "gridcell",
    "checkbox",
    "radio",
    "switch",
    "slider",
    "textbox",
    "combobox"
  ]);
  const structureTags = new Set(["NAV", "HEADER", "FOOTER", "MAIN", "ASIDE", "SECTION", "ARTICLE", "FORM", "TABLE", "UL", "OL", "LI"]);
  const landmarkRoles = new Set(["navigation", "banner", "contentinfo", "main", "complementary", "region", "article"]);
  const mediaTags = new Set(["IMG", "SVG", "CANVAS", "VIDEO", "PICTURE", "IFRAME"]);
  const headingTags = new Set(["H1", "H2", "H3", "H4", "H5", "H6"]);
  const hardSkipTags = new Set(["META", "SCRIPT", "STYLE", "LINK", "TITLE", "HEAD", "NOSCRIPT", "BR"]);
  const stableAttrs = [
    "data-testid",
    "data-test",
    "data-qa",
    "data-cy",
    "data-automation",
    "data-eid",
    "data-id",
    "data-action",
    "data-target",
    "data-name",
    "dt-eid",
    "dt-imp-once",
    "dt-params",
    "aria-label",
    "aria-labelledby",
    "name",
    "title",
    "alt"
  ];
  const preferredDataAttrs = ["data-testid", "data-test", "data-qa", "data-cy", "data-automation"];
  const semanticWords = new Set([
    "search",
    "login",
    "signin",
    "register",
    "signup",
    "username",
    "password",
    "captcha",
    "code",
    "filter",
    "price",
    "brand",
    "logout",
    "submit",
    "save",
    "confirm",
    "cancel",
    "close",
    "more",
    "menu",
    "dialog",
    "tab",
    "button",
    "link",
    "edit",
    "delete",
    "remove",
    "add",
    "create",
    "select",
    "sort",
    "toggle",
    "title",
    "image",
    "img",
    "store",
    "mall",
    "subsidy",
    "detail",
    "goods",
    "product",
    "card",
    "collect",
    "name",
    "year",
    "mileage",
    "city",
    "location",
    "address",
    "tag",
    "tags",
    "badge",
    "meta",
    "info",
    "amount",
    "reduction",
    "panel",
    "section"
  ]);
  const weakNoiseWords = new Set([
    "wrapper",
    "container",
    "inner",
    "outer",
    "content",
    "box",
    "group",
    "flex",
    "row",
    "col",
    "grid",
    "inline",
    "block",
    "layout",
    "item",
    "root",
    "main"
  ]);

  const cssString = (value) => `"${String(value)
    .replace(/\\/g, "\\\\")
    .replace(/"/g, '\\"')
    .replace(/\r/g, "\\d ")
    .replace(/\n/g, "\\a ")
    .replace(/\f/g, "\\c ")}"`;
  const attrSelector = (name, value) => `[${name}=${cssString(value)}]`;
  const normalizeText = (value) =>
    String(value || "")
      .normalize("NFKC")
      .replace(/\s+/g, " ")
      .trim();
  const directText = (el) =>
    normalizeText(
      Array.from(el.childNodes || [])
        .filter((node) => node.nodeType === Node.TEXT_NODE)
        .map((node) => node.textContent || "")
        .join(" ")
    );
  const visibleText = (el) =>
    normalizeText(el.innerText || el.value || el.getAttribute("aria-label") || el.getAttribute("alt") || el.getAttribute("title") || el.textContent || "");
  const roleOf = (el) =>
    el.getAttribute("role") ||
    {
      A: "link",
      BUTTON: "button",
      INPUT: inputRole(el),
      SELECT: "combobox",
      TEXTAREA: "textbox",
      H1: "heading",
      H2: "heading",
      H3: "heading",
      H4: "heading",
      H5: "heading",
      H6: "heading",
      NAV: "navigation",
      HEADER: "banner",
      FOOTER: "contentinfo",
      MAIN: "main",
      ASIDE: "complementary",
      SECTION: "region",
      ARTICLE: "article"
    }[el.tagName] ||
    "";

  function inputRole(el) {
    const type = String(el.getAttribute("type") || "text").toLowerCase();
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (["button", "submit", "reset"].includes(type)) return "button";
    if (type === "range") return "slider";
    return "textbox";
  }

  let queryRootsCache = null;

  function queryRoots() {
    if (queryRootsCache) return queryRootsCache;
    const roots = [document];
    for (let index = 0; index < roots.length; index += 1) {
      const root = roots[index];
      for (const el of Array.from(root.querySelectorAll ? root.querySelectorAll("*") : [])) {
        if (el.shadowRoot && !roots.includes(el.shadowRoot)) roots.push(el.shadowRoot);
      }
    }
    queryRootsCache = roots;
    return roots;
  }

  function deepQueryAll(selector) {
    const output = [];
    const seen = new Set();
    for (const root of queryRoots()) {
      let nodes = [];
      try {
        nodes = Array.from(root.querySelectorAll(selector));
      } catch {
        return [];
      }
      for (const node of nodes) {
        if (!seen.has(node)) {
          seen.add(node);
          output.push(node);
        }
      }
    }
    return output;
  }

  function cssCount(selector) {
    try {
      return deepQueryAll(selector).length;
    } catch {
      return 0;
    }
  }

  function exactVisibleTextCount(text) {
    if (!text) return 0;
    let count = 0;
    for (const root of queryRoots()) {
      const walker = document.createTreeWalker(root === document ? (document.body || document.documentElement) : root, NodeFilter.SHOW_ELEMENT);
      let node = walker.nextNode();
      while (node) {
        if (visibleText(node) === text) count += 1;
        if (count > 1) return count;
        node = walker.nextNode();
      }
    }
    return count;
  }

  function hasStableAttribute(el) {
    return stableAttrs.some((name) => {
      const value = el.getAttribute(name);
      return value && isStableValue(value);
    });
  }

  function isVisible(el) {
    if (!el || hardSkipTags.has(el.tagName) || el.closest("head")) return false;
    if (el.hidden || el.getAttribute("aria-hidden") === "true") return false;
    const style = getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
    let parent = el.parentElement;
    while (parent) {
      const parentStyle = getComputedStyle(parent);
      if (parentStyle.display === "none" || parentStyle.visibility === "hidden" || Number(parentStyle.opacity) === 0) return false;
      parent = parent.parentElement;
    }
    const rect = el.getBoundingClientRect();
    return rect.width >= 8 && rect.height >= 8;
  }

  function isDynamicToken(token) {
    const value = String(token || "");
    if (!value) return true;
    if (/^(react|mui|ember|ng|jss|css|ant|rc|ruyi|icon|swiper|slick|splide)-/i.test(value)) return true;
    if (/^[a-f0-9]{8,}$/i.test(value)) return true;
    if (/^[A-Za-z0-9_-]{12,}$/.test(value) && /[A-Z]/.test(value) && /\d/.test(value)) return true;
    if (/\d{4,}/.test(value)) return true;
    return false;
  }

  function isStableValue(value) {
    const text = normalizeText(value);
    if (!text || text.length > 96) return false;
    if (/^[0-9a-f]{8,}$/i.test(text)) return false;
    if (/^\d{5,}$/.test(text)) return false;
    return true;
  }

  function classIdText(el) {
    return `${el.id || ""} ${el.getAttribute("class") || ""}`;
  }

  function rawTokens(value) {
    return normalizeText(value)
      .replace(/([a-z])([A-Z])/g, "$1 $2")
      .split(/[^A-Za-z0-9\u4e00-\u9fff]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function semanticTokens(el) {
    return rawTokens(classIdText(el))
      .map((item) => item.toLowerCase())
      .filter((item) => item && !isDynamicToken(item) && !weakNoiseWords.has(item) && !/^([pm]|text|bg|border|rounded|shadow|opacity|z)-/.test(item))
      .slice(0, 5);
  }

  function hasSemanticClass(el) {
    return semanticTokens(el).some((token) => semanticWords.has(token));
  }

  function semanticClassFragments(el) {
    const fragments = [];
    for (const className of Array.from(el.classList || [])) {
      const rawClassName = String(className);
      if (/^(?:css|jss|mui|ant|rc|ruyi|ember|ng|swiper|slick|splide)-/i.test(rawClassName) || /^[a-f0-9]{8,}$/i.test(rawClassName)) continue;
      // Strip only the CSS-module build suffix. BEM block/element names are
      // indivisible roles: splitting on underscores merges cards and fields.
      const normalizedClassName = rawClassName.replace(/^_+/, "")
        .replace(/_[A-Za-z0-9]+_\d+$/, "").split(/--/)[0];
      const segments = [normalizedClassName];
      for (const segment of segments) {
        const normalizedSegment = segment.toLowerCase();
        const tokens = rawTokens(segment).map((item) => item.toLowerCase());
        const meaningful = tokens.filter((token) => !weakNoiseWords.has(token) && !["styles", "style", "module", "css", "scoped"].includes(token));
        if (!meaningful.length) continue;
        if (/^(?:[a-z]*\d+){2,}[a-z]*$/i.test(normalizedSegment) || /^[a-f0-9]{6,}$/i.test(normalizedSegment)) continue;
        const hasKnownSemanticWord = meaningful.some((token) => semanticWords.has(token));
        if (!hasKnownSemanticWord && meaningful.length < 2 && normalizedSegment.length < 6) continue;
        if (!hasKnownSemanticWord && meaningful.length < 2 && normalizedSegment.length < 8) continue;
        if (!fragments.some((item) => item.value === segment)) {
          fragments.push({ value: segment, score: (hasKnownSemanticWord ? 10 : 0) + meaningful.length * 2 + Math.min(normalizedSegment.length, 20) / 20 });
        }
      }
    }
    return fragments.sort((left, right) => right.score - left.score).slice(0, 3).map((item) => item.value);
  }

  function semanticClassSelector(el, fragment) {
    const tag = el.tagName.toLowerCase();
    if (el.classList.contains(fragment)) return `${tag}.${CSS.escape(fragment)}`;
    for (const className of Array.from(el.classList || [])) {
      const module = String(className).match(/^(_*)(.+?)_[A-Za-z0-9]+_\d+$/);
      if (module && module[2].split(/--/)[0] === fragment) {
        const prefix = `${module[1]}${fragment}_`;
        // Match a class token prefix, never a substring of a different role.
        return `${tag}:is([class^=${cssString(prefix)}],[class*=${cssString(` ${prefix}`)}])` +
          `:not([class*=${cssString(`${prefix}_`)}])`;
      }
    }
    return "";
  }

  function semanticLabel(el, kind) {
    const words = new Set(rawTokens(classIdText(el)).map((item) => item.toLowerCase()));
    const has = (...items) => items.some((item) => words.has(item));
    if (has("title", "name") && has("card", "product", "goods")) return "名称区域";
    if (has("tag", "tags", "badge")) return "标签区域";
    if (has("reduction", "subsidy")) return "优惠区域";
    if (has("price", "amount")) return "价格区域";
    if (has("detail", "meta", "info")) return "详情区域";
    if (has("city", "location", "address")) return "位置区域";
    if (has("filter", "sort")) return "筛选区域";
    if (has("year", "mileage")) {
      const segments = visibleText(el).split(/[\/|·•]/).map(normalizeText).filter(Boolean);
      const hasAdministrativeRegion = segments.some((segment) => /[\u4e00-\u9fff]{2,}(?:省|市|区|县|州|盟|旗)$/.test(segment));
      return hasAdministrativeRegion ? "城市 / 里程区域" : "里程区域";
    }
    if (kind === "media" || has("image", "img")) return "图片区域";
    if (has("collect", "card", "product", "goods") && !has("wrapper", "container")) return "卡片区域";
    if (has("title", "name")) return "名称区域";
    return "";
  }

  function isButtonLike(el) {
    if (!["DIV", "SPAN"].includes(el.tagName)) return false;
    const role = roleOf(el);
    if (role === "button") return true;
    const tokens = semanticTokens(el);
    return tokens.includes("button") || tokens.includes("btn") || (tokens.includes("filter") && tokens.includes("button"));
  }

  function isPointerSurface(el) {
    if (getComputedStyle(el).cursor !== "pointer") return false;
    const parent = el.parentElement;
    return !parent || getComputedStyle(parent).cursor !== "pointer";
  }

  function isNativeInteractive(el) {
    return nativeInteractiveTags.has(el.tagName) || roleInteractive.has(roleOf(el));
  }

  function isKeyNode(el) {
    if (isNativeInteractive(el) || isButtonLike(el)) return true;
    const role = roleOf(el);
    const tabindex = Number(el.getAttribute("tabindex"));
    const style = getComputedStyle(el);
    const ownText = directText(el);
    const isScrollable = el.scrollHeight > el.clientHeight + 8 || el.scrollWidth > el.clientWidth + 8;
    return (
      (!Number.isNaN(tabindex) && tabindex >= 0) ||
      el.hasAttribute("aria-pressed") ||
      el.hasAttribute("aria-selected") ||
      el.hasAttribute("aria-expanded") ||
      el.getAttribute("contenteditable") === "true" ||
      Boolean(el.onclick || el.getAttribute("onclick")) ||
      style.cursor === "pointer" ||
      Boolean(el.getAttribute("aria-label") || el.getAttribute("aria-labelledby") || el.getAttribute("title") || el.getAttribute("alt")) ||
      (ownText.length >= 2 && ownText.length <= 24) ||
      el.children.length >= 2 ||
      (el.parentElement && el.parentElement.children.length >= 2) ||
      structureTags.has(el.tagName) ||
      landmarkRoles.has(role) ||
      hasStableAttribute(el) ||
      hasSemanticClass(el) ||
      isScrollable ||
      ["sticky", "fixed"].includes(style.position) ||
      ["dialog", "alertdialog"].includes(role)
    );
  }

  function isIndependentInteractive(el) {
    return (
      isNativeInteractive(el) ||
      isButtonLike(el) ||
      isPointerSurface(el) ||
      Boolean(el.onclick || el.getAttribute("onclick")) ||
      roleInteractive.has(roleOf(el))
    );
  }

  function hasInteractiveAncestor(el) {
    let parent = parentElementOrHost(el);
    while (parent && parent !== document.body && parent !== document.documentElement) {
      if ((isNativeInteractive(parent) || isButtonLike(parent) || isPointerSurface(parent)) && !isIndependentInteractive(el)) return true;
      parent = parentElementOrHost(parent);
    }
    return false;
  }

  function stableClassSelector(el) {
    const classes = Array.from(el.classList || []).filter((item) => {
      const lower = item.toLowerCase();
      if (isDynamicToken(lower)) return false;
      if (/^(ant|css|rc|jss|mui|ruyi|ember|ng|swiper|slick|splide)-/.test(lower)) return false;
      return rawTokens(lower).some((token) => semanticWords.has(token.toLowerCase())) || lower.length <= 24;
    });
    return classes.slice(0, 2).map((item) => `.${CSS.escape(item)}`).join("");
  }

  function cssPath(el) {
    const parts = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement) {
      const tag = current.tagName.toLowerCase();
      let part = tag;
      const id = current.id;
      if (id && isStableValue(id) && !isDynamicToken(id)) {
        const selector = `#${CSS.escape(id)}`;
        if (cssCount(selector) === 1) {
          parts.unshift(selector);
          break;
        }
      }
      const stableClasses = stableClassSelector(current);
      part += stableClasses;
      if (stableClasses && cssCount(part) === 1) {
        parts.unshift(part);
        return parts.join(" > ");
      }
      const parent = current.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((child) => child.tagName === current.tagName);
        if (same.length > 1) part += `:nth-of-type(${same.indexOf(current) + 1})`;
      }
      parts.unshift(part);
      const selector = parts.join(" > ");
      if (cssCount(selector) === 1) return selector;
      current = parent;
    }
    return parts.join(" > ") || el.tagName.toLowerCase();
  }

  function accessibleName(el) {
    const labelledBy = el.getAttribute("aria-labelledby");
    if (labelledBy) {
      const text = labelledBy
        .split(/\s+/)
        .map((id) => document.getElementById(id)?.innerText || document.getElementById(id)?.textContent || "")
        .join(" ");
      if (normalizeText(text)) return normalizeText(text);
    }
    const aria = el.getAttribute("aria-label");
    if (aria) return normalizeText(aria);
    if (el.id) {
      const label = document.querySelector(`label[for=${JSON.stringify(el.id)}]`);
      if (label) return visibleText(label);
    }
    for (const attr of ["title", "alt", "value", "placeholder"]) {
      const value = el.getAttribute(attr) || (attr === "value" ? el.value : "");
      if (value) return normalizeText(value);
    }
    return visibleText(el);
  }

  let roleNameCountCache = null;

  function roleNameCount(role, name) {
    if (!roleNameCountCache) {
      roleNameCountCache = new Map();
      for (const item of deepQueryAll("*")) {
        const itemRole = roleOf(item);
        if (!itemRole || !roleInteractive.has(itemRole)) continue;
        const itemName = accessibleName(item);
        if (!itemName || itemName.length > 60) continue;
        const key = `${itemRole}\u0000${itemName}`;
        roleNameCountCache.set(key, (roleNameCountCache.get(key) || 0) + 1);
      }
    }
    return roleNameCountCache.get(`${role}\u0000${name}`) || 0;
  }

  function locatorScore(type, selector, unique) {
    let score = 36;
    const value = String(selector || "");
    if (["testid", "data", "dt"].includes(type)) score += 42;
    if (type === "name") score += 38;
    if (type === "id") score += 36;
    if (type === "label" || type === "role" || type === "aria" || type === "title") score += 31;
    if (type === "class") score += 28;
    if (type === "css") score += 20;
    if (type === "text") score += 2;
    if (unique) score += 14;
    if (/nth-(child|of-type)|\[\d+\]/.test(value)) score -= 16;
    if ((value.match(/>/g) || []).length > 2) score -= 12;
    if (value.length > 96) score -= 14;
    if (value.length > 150) score -= 20;
    if (/^text=/.test(value) && /[\u4e00-\u9fff]/.test(value)) score -= 4;
    return Math.max(1, Math.min(100, Math.round(score)));
  }

  function addLocator(list, type, selector, unique, explicitMatchCount = undefined) {
    if (!selector || list.some((item) => item.selector === selector)) return;
    if (/^xpath=\/(?:html|body)/.test(selector)) return;
    const score = locatorScore(type, selector, unique);
    const matchCount = typeof explicitMatchCount === "number"
      ? explicitMatchCount
      : (["role", "text", "label"].includes(type) ? (unique ? 1 : null) : cssCount(selector));
    if (matchCount === 0) return;
    list.push({ type, selector, unique, matchCount, score, displayScore: score + typePreference(type) });
  }

  function typePreference(type) {
    return { scoped: 8, role: 6, testid: 6, name: 5, label: 4, id: 4, class: 4, css: 4, data: 4, dt: 4, aria: 3, title: 3, placeholder: 2, text: 1 }[type] || 0;
  }

  function parentElementOrHost(el) {
    if (el.parentElement) return el.parentElement;
    const rootNode = el.getRootNode ? el.getRootNode() : null;
    return rootNode && rootNode.host ? rootNode.host : null;
  }

  function repeatedClassOption(el) {
    let best = null;
    for (const fragment of semanticClassFragments(el)) {
      const selector = semanticClassSelector(el, fragment);
      if (!selector) continue;
      const count = cssCount(selector);
      if (count < 2 || count > 240) continue;
      const semantic = rawTokens(fragment).some((token) => semanticWords.has(token.toLowerCase()));
      const candidate = { selector, count, fragment, score: (semantic ? 20 : 0) + Math.min(20, fragment.length) };
      if (!best || candidate.score > best.score) best = candidate;
    }
    return best;
  }

  function repeatedScopeFor(el) {
    let parent = parentElementOrHost(el);
    let fallback = null;
    for (let depth = 0; parent && depth < 8; depth += 1) {
      const option = repeatedClassOption(parent);
      if (option) {
        const words = new Set(rawTokens(classIdText(parent)).map((item) => item.toLowerCase()));
        const cardLike = ["card", "item", "product", "goods", "record", "row"].some((word) => words.has(word));
        const scope = { el: parent, selector: option.selector, count: option.count, familyId: `${parent.tagName.toLowerCase()}:${option.fragment}` };
        if (cardLike) return scope;
        if (!fallback) fallback = scope;
      }
      parent = parentElementOrHost(parent);
    }
    return fallback;
  }

  function relativeSelectorFor(el) {
    const repeated = repeatedClassOption(el);
    if (repeated) return repeated.selector;
    for (const fragment of semanticClassFragments(el)) {
      const selector = semanticClassSelector(el, fragment);
      if (selector && cssCount(selector) > 0) return selector;
    }
    return "";
  }

  function generateLocators(el, kind) {
    const list = [];
    for (const attr of preferredDataAttrs) {
      const value = el.getAttribute(attr);
      if (value && isStableValue(value)) {
        const selector = attrSelector(attr, value);
        addLocator(list, "testid", selector, cssCount(selector) === 1);
      }
    }
    const dt = el.getAttribute("dt-eid");
    if (dt && isStableValue(dt)) {
      const selector = attrSelector("dt-eid", dt);
      addLocator(list, "dt", selector, cssCount(selector) === 1);
    }
    for (const attr of Array.from(el.getAttributeNames ? el.getAttributeNames() : [])) {
      if (!attr.startsWith("data-") || preferredDataAttrs.includes(attr)) continue;
      const value = el.getAttribute(attr);
      if (value && isStableValue(value)) {
        const selector = attrSelector(attr, value);
        addLocator(list, "data", selector, cssCount(selector) === 1);
      }
    }
    if (el.id && isStableValue(el.id) && !isDynamicToken(el.id)) {
      const selector = `#${CSS.escape(el.id)}`;
      addLocator(list, "id", selector, cssCount(selector) === 1);
    }
    const name = el.getAttribute("name");
    if (name && isStableValue(name)) {
      const selector = `${el.tagName.toLowerCase()}${attrSelector("name", name)}`;
      addLocator(list, "name", selector, cssCount(selector) === 1);
    }
    const placeholder = el.getAttribute("placeholder");
    if (placeholder && isStableValue(placeholder)) {
      const selector = `${el.tagName.toLowerCase()}${attrSelector("placeholder", placeholder)}`;
      addLocator(list, "placeholder", selector, cssCount(selector) === 1);
    }
    const aria = el.getAttribute("aria-label");
    if (aria && isStableValue(aria)) {
      const selector = attrSelector("aria-label", aria);
      addLocator(list, "aria", selector, cssCount(selector) === 1);
    }
    const role = roleOf(el);
    const acc = accessibleName(el);
    if (role && acc && acc.length <= 60 && roleInteractive.has(role)) {
      const roleCount = roleNameCount(role, acc);
      addLocator(list, "role", `role=${role}[name=${JSON.stringify(acc)}]`, roleCount === 1, roleCount);
    }
    const alt = el.getAttribute("alt");
    if (alt && isStableValue(alt)) {
      const selector = `${el.tagName.toLowerCase()}${attrSelector("alt", alt)}`;
      addLocator(list, "aria", selector, cssCount(selector) === 1);
    }
    const title = el.getAttribute("title");
    if (title && isStableValue(title)) {
      const selector = `${el.tagName.toLowerCase()}${attrSelector("title", title)}`;
      addLocator(list, "title", selector, cssCount(selector) === 1);
    }
    for (const fragment of semanticClassFragments(el)) {
      const selector = semanticClassSelector(el, fragment);
      if (selector) addLocator(list, "class", selector, cssCount(selector) === 1);
    }
    const text = visibleText(el);
    if (text && text.length <= 60 && kind !== "component" && exactVisibleTextCount(text) === 1) {
      addLocator(list, "text", `text=${JSON.stringify(text)}`, true);
    }
    const css = cssPath(el);
    addLocator(list, "css", css, cssCount(css) === 1);

    const byType = new Map();
    for (const item of list.sort((left, right) => right.displayScore - left.displayScore)) {
      const existing = byType.get(item.type);
      if (!existing || item.displayScore > existing.displayScore) byType.set(item.type, item);
    }
    const ranked = Array.from(byType.values()).sort((left, right) => right.displayScore - left.displayScore);
    const scope = repeatedScopeFor(el);
    const relativeSelector = scope ? relativeSelectorFor(el) : "";
    if (scope && relativeSelector) {
      const scopedSelector = `${scope.selector} ${relativeSelector}`;
      const scopedCount = cssCount(scopedSelector);
      const scopes = deepQueryAll(scope.selector);
      const peerScopes = scopes.every((node) => !scopes.some((other) => other !== node && other.contains(node)));
      const exactFields = scopes.every((node) => node.querySelectorAll(relativeSelector).length === 1);
      if (scopedCount === scopes.length && peerScopes && exactFields &&
          scope.el.querySelector(relativeSelector) === el) {
        ranked.unshift({
          type: "scoped",
          selector: scopedSelector,
          unique: scopedCount === 1,
          matchCount: scopedCount,
          score: 82,
          displayScore: 90,
          scopeSelector: scope.selector,
          relativeSelector,
          matchMode: "every_scope",
          familyId: scope.familyId
        });
      }
    }
    ranked.sort((left, right) => right.displayScore - left.displayScore);
    const repeatedStructural = ranked.find((item) => ["scoped", "class"].includes(item.type) && Number(item.matchCount || 0) >= 2);
    const semanticRegion = Boolean(semanticLabel(el, kind));
    const best = (semanticRegion && repeatedStructural) || (kind === "interactive"
      ? ranked.find((item) => item.unique && item.type !== "class") || ranked[0]
      : ranked[0]);
    const fallback = { type: "css", selector: css, unique: false, matchCount: cssCount(css), score: 35, displayScore: 35 };
    const ordered = best ? [best, ...ranked.filter((item) => item.selector !== best.selector)] : ranked;
    const options = ordered
      .map((item) => ({
        selector: item.selector,
        selector_type: item.type,
        stability: stability(item.score),
        score: item.score,
        unique: item.unique,
        match_count: item.matchCount,
        scope_selector: item.scopeSelector,
        relative_selector: item.relativeSelector,
        match_mode: item.matchMode || (Number(item.matchCount || 0) > 1 ? "all" : "first"),
        count_mode: "visible",
        family_id: item.familyId
      }));
    return { best: best || fallback, options: options.length ? options : [{
      selector: fallback.selector,
      selector_type: fallback.type,
      stability: stability(fallback.score),
      score: fallback.score,
      unique: fallback.unique,
      match_count: fallback.matchCount,
      match_mode: Number(fallback.matchCount || 0) > 1 ? "all" : "first",
      count_mode: "visible"
    }] };
  }

  function cleanBase(value) {
    const text = normalizeText(value).replace(/[([]?\d+[)\]]?$/, "");
    if (!text) return "";
    const cleaned = text
      .replace(/[^\u4e00-\u9fffA-Za-z0-9.%]+/g, "_")
      .replace(/_+/g, "_")
      .replace(/^_+|_+$/g, "");
    const tokens = cleaned.split("_").filter(Boolean);
    if (!tokens.length) return "";
    const deduped = [];
    for (const token of tokens) {
      if (deduped[deduped.length - 1] !== token) deduped.push(token);
    }
    const hasCjk = deduped.some((token) => /[\u4e00-\u9fff]/.test(token));
    const preferred = hasCjk ? deduped.filter((token) => /[\u4e00-\u9fff0-9]/.test(token)) : deduped;
    return preferred
      .slice(0, 3)
      .map((token) => (token.length > 18 ? token.slice(0, 18) : token))
      .join("_");
  }

  function baseFor(el, category) {
    const candidates = [];
    const acc = accessibleName(el);
    if (category === "buttonlike") {
      candidates.push(directText(el), acc, el.getAttribute("title"), semanticTokens(el).join("_"));
    } else if (category === "interactive") {
      candidates.push(acc, el.getAttribute("title"), el.value, el.getAttribute("placeholder"), visibleText(el), el.getAttribute("name"), semanticTokens(el).join("_"));
    } else {
      candidates.push(directText(el), visibleText(el), roleOf(el), semanticTokens(el).join("_"));
    }
    const chinese = candidates.map(cleanBase).find((item) => /[\u4e00-\u9fff]/.test(item));
    return chinese || candidates.map(cleanBase).find(Boolean) || "element";
  }

  function buildName(el, kind) {
    const label = semanticLabel(el, kind);
    const category = isButtonLike(el) ? "buttonlike" : isNativeInteractive(el) ? "interactive" : "container";
    const fallback = kind === "media" ? "图片区域" : kind === "interactive" ? "操作元素" : kind === "text" ? "文本" : "内容区域";
    const base = cleanBase(label) || baseFor(el, category) || fallback;
    return (base === "element" ? fallback : base).slice(0, 35);
  }

  function kindOf(el) {
    if (mediaTags.has(el.tagName)) return "media";
    if (isNativeInteractive(el) || isButtonLike(el) || isPointerSurface(el)) return "interactive";
    const own = directText(el);
    if (headingTags.has(el.tagName) || (own && own.length <= 60 && el.children.length <= 1)) return "text";
    if (structureTags.has(el.tagName) || landmarkRoles.has(roleOf(el)) || hasStableAttribute(el) || hasSemanticClass(el)) return "component";
    return visibleText(el) ? "text" : "component";
  }

  function elementScore(el, kind, locator, rect) {
    let score = locator.displayScore;
    if (locator.type === "class" && Number(locator.matchCount || 0) >= 2) score += 14;
    if (kind === "interactive") score += 30;
    if (kind === "media") score += 22;
    if (kind === "component") score += 10;
    if (headingTags.has(el.tagName)) score += 12;
    const area = rect.width * rect.height;
    score += Math.min(12, Math.log10(Math.max(area, 1)) * 3);
    if (area > pageWidth * pageHeight * 0.68) score -= 24;
    if (rect.width < 24 || rect.height < 16) score -= 24;
    return Math.round(score);
  }

  function stability(score) {
    if (score >= 80) return "high";
    if (score >= 60) return "medium";
    return "low";
  }

  function selectorCollector() {
    const classKeywords = [
      "btn",
      "button",
      "tab",
      "link",
      "icon",
      "close",
      "clear",
      "remove",
      "delete",
      "edit",
      "add",
      "create",
      "submit",
      "confirm",
      "cancel",
      "search",
      "filter",
      "price",
      "sort",
      "toggle",
      "select",
      "title",
      "image",
      "img",
      "store",
      "mall",
      "subsidy",
      "detail",
      "goods",
      "product",
      "card",
      "name",
      "year",
      "mileage",
      "city",
      "location",
      "tag",
      "tags",
      "badge",
      "meta",
      "info",
      "amount",
      "panel",
      "section"
    ];
    return [
      "button",
      "a[href]",
      "input:not([type='hidden'])",
      "select",
      "textarea",
      "[role='button']",
      "[role='link']",
      "[role='menuitem']",
      "[role='tab']",
      "[role='option']",
      "[role='treeitem']",
      "[role='listitem']",
      "[role='row']",
      "[role='cell']",
      "[role='gridcell']",
      "[role='checkbox']",
      "[role='radio']",
      "[role='textbox']",
      "[role='combobox']",
      "[onclick]",
      "[tabindex]:not([tabindex='-1'])",
      "label[for]",
      "img[alt]",
      "[data-testid]",
      "[data-test]",
      "[data-qa]",
      "[data-cy]",
      "[data-automation]",
      "[data-eid]",
      "[data-id]",
      "[data-action]",
      "[data-target]",
      "[data-name]",
      "[dt-eid]",
      "[dt-imp-once]",
      "[dt-params]",
      "[aria-label]",
      "[aria-labelledby]",
      "[name]",
      "[title]",
      "main",
      "nav",
      "header",
      "footer",
      "section",
      "article",
      "aside",
      "form",
      "table",
      "ul",
      "ol",
      "li",
      "h1",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "img",
      "svg",
      "canvas",
      "video",
      "picture",
      "iframe",
      ...classKeywords.flatMap((word) => [`[class*='${word}']`, `[id*='${word}']`])
    ].join(",");
  }

  const collectedNodes = deepQueryAll(selectorCollector());
  const repeatedSelectorNodes = new Map();
  const repeatedClassNodes = [];
  for (const el of deepQueryAll("[class]").slice(0, 4000)) {
    const text = visibleText(el);
    if (!text || text.length > 240) continue;
    for (const fragment of semanticClassFragments(el)) {
      const selector = semanticClassSelector(el, fragment);
      if (!selector) continue;
      const nodes = repeatedSelectorNodes.get(selector) || [];
      nodes.push(el);
      repeatedSelectorNodes.set(selector, nodes);
    }
  }
  for (const nodes of repeatedSelectorNodes.values()) {
    if (nodes.length < 2 || nodes.length > 240) continue;
    repeatedClassNodes.push(...nodes);
    if (repeatedClassNodes.length >= 520) break;
  }
  const rawNodes = [];
  for (const el of [...collectedNodes, ...repeatedClassNodes]) {
    rawNodes.push(el);
    let parent = parentElementOrHost(el);
    while (parent && parent !== document.body && parent !== document.documentElement) {
      if (isPointerSurface(parent)) rawNodes.push(parent);
      parent = parentElementOrHost(parent);
    }
  }
  const eligibleNodes = [];
  const seenNodes = new Set();
  for (const el of rawNodes) {
    if (seenNodes.has(el) || !isVisible(el) || !isKeyNode(el) || hasInteractiveAncestor(el)) continue;
    seenNodes.add(el);
    const kind = kindOf(el);
    const semantic = hasSemanticClass(el) || kind === "media" || headingTags.has(el.tagName);
    const preferred = preferredDataAttrs.some((attr) => Boolean(el.getAttribute(attr))) || Boolean(el.id && isStableValue(el.id) && !isDynamicToken(el.id));
    const interactive = kind === "interactive";
    const priority = (preferred ? 120 : 0) + (semantic ? 90 : 0) + (interactive ? 60 : 0) + (hasStableAttribute(el) ? 30 : 0);
    eligibleNodes.push({ el, semantic, preferred, interactive, priority });
  }

  const selectedNodes = [];
  const selectedSet = new Set();
  const takePool = (pool, limit) => {
    for (const item of pool.sort((left, right) => right.priority - left.priority).slice(0, limit)) {
      if (!selectedSet.has(item.el)) {
        selectedSet.add(item.el);
        selectedNodes.push(item.el);
      }
    }
  };
  takePool(eligibleNodes.filter((item) => item.semantic), 220);
  takePool(eligibleNodes.filter((item) => item.preferred), 100);
  takePool(eligibleNodes.filter((item) => item.interactive), 160);
  takePool(eligibleNodes.filter((item) => !item.semantic && !item.preferred && !item.interactive), 40);
  if (selectedNodes.length < MAX_WORK_ITEMS) takePool(eligibleNodes, MAX_WORK_ITEMS - selectedNodes.length);
  const visibleNodes = selectedNodes.slice(0, MAX_WORK_ITEMS);

  const initial = [];
  for (const el of visibleNodes) {
    const rect = el.getBoundingClientRect();
    const x = Math.max(0, rect.left + window.scrollX);
    const y = Math.max(0, rect.top + window.scrollY);
    if (x > pageWidth || y > pageHeight) continue;
    const kind = kindOf(el);
    const locators = generateLocators(el, kind);
    const locator = locators.best;
    const score = elementScore(el, kind, locator, rect);
    initial.push({
      el,
      selector: locator.selector,
      selector_type: locator.type,
      stability: stability(locator.score),
      rank_score: score,
      score: locator.score,
      name: buildName(el, kind),
      semantic_label: semanticLabel(el, kind),
      selector_options: locators.options,
      scope_selector: locator.scopeSelector,
      relative_selector: locator.relativeSelector,
      match_mode: locator.matchMode || (Number(locator.matchCount || 0) > 1 ? "all" : "first"),
      count_mode: "visible",
      family_id: locator.familyId,
      text: visibleText(el).slice(0, 160),
      tag: el.tagName.toLowerCase(),
      role: roleOf(el),
      kind,
      box: {
        x,
        y,
        width: Math.min(pageWidth - x, rect.width),
        height: Math.min(pageHeight - y, rect.height)
      }
    });
  }

  const pruned = initial.filter((item) => {
    if (item.semantic_label && (hasSemanticClass(item.el) || item.kind === "media" || headingTags.has(item.el.tagName))) return true;
    return !initial.some((other) => {
      if (item === other) return false;
      if (!item.el.contains(other.el)) return false;
      const itemText = normalizeText(item.text);
      const otherText = normalizeText(other.text);
      return otherText && (itemText === otherText || itemText.includes(otherText));
    });
  });

  const bySelector = new Map();
  for (const item of pruned.sort((left, right) => right.rank_score - left.rank_score)) {
    if (!bySelector.has(item.selector)) bySelector.set(item.selector, item);
  }

  function candidateFamily(item) {
    const classSelector = stableClassSelector(item.el);
    if (classSelector) return `${item.kind}:${item.el.tagName.toLowerCase()}${classSelector}`;
    return `${item.kind}:${item.selector.replace(/:nth-(?:child|of-type)\(\d+\)/g, ":nth(*)")}`;
  }

  const familyCounts = new Map();
  const familyLimited = [];
  for (const item of Array.from(bySelector.values()).sort((left, right) => right.rank_score - left.rank_score)) {
    const family = candidateFamily(item);
    const count = familyCounts.get(family) || 0;
    const limit = item.stability === "low" ? (item.kind === "interactive" ? 8 : 3) : MAX_RESULTS;
    if (count >= limit) continue;
    familyCounts.set(family, count + 1);
    familyLimited.push(item);
  }

  const nameCounts = new Map();
  const output = familyLimited
    .slice(0, MAX_RESULTS)
    .map(({ el, rank_score, ...item }, index) => {
      const count = (nameCounts.get(item.name) || 0) + 1;
      nameCounts.set(item.name, count);
      return { ...item, _rank_score: rank_score, candidate_id: `candidate-${index + 1}`, name: count === 1 ? item.name : `${item.name} ${count}` };
    });

  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    page_size: { width: pageWidth, height: pageHeight },
    candidates: output
  };
}
"""
