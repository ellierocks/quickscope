// Adds a "Quickscope" button next to Play on a game's library page.
//
// Steam builds the Play row several components deep (page > ... > play
// section > ... > ActionRow), and those components have no public names. Every
// component on the way down carries the app overview plus `setSections` (or
// `onGameInfoButtonToggle` at the top), so rather than hard-coding the chain we
// follow components with those props until one renders the ActionRow, then
// insert the button after Play. If Steam's layout changes and the row can't be
// found, nothing is added and the page renders as normal.
import { routerHook } from "@decky/api";
import {
  DialogButton,
  afterPatch,
  basicAppDetailsSectionStylerClasses,
  findInReactTree,
  wrapReactClass,
  wrapReactType,
} from "@decky/ui";
import { useState } from "react";
import { FaCrosshairs } from "react-icons/fa";

import { launch, toast } from "./launch";
import { isLaunchable, toLibraryApp } from "./library";

const ROUTE = "/library/app/:appid";
// The Play row is 5 levels below the page today; leave room for Steam adding a few.
const MAX_DEPTH = 10;
const BUTTON_KEY = "quickscope-launch";

type Handler = (args: any[], ret: any) => any;

function QuickscopeButton({ overview }: { overview: any }) {
  const [busy, setBusy] = useState(false);
  const onClick = async () => {
    const app = toLibraryApp(overview);
    if (!app) return;
    setBusy(true);
    try {
      await launch(app);
    } catch (e) {
      toast(`Launch failed: ${e}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <DialogButton
      key={BUTTON_KEY}
      disabled={busy}
      onClick={onClick}
      style={{
        width: "auto",
        minWidth: 0,
        height: "48px",
        marginLeft: "12px",
        padding: "0 20px",
        display: "flex",
        alignItems: "center",
        gap: "10px",
        flexShrink: 0,
      }}
    >
      <FaCrosshairs />
      Quickscope
    </DialogButton>
  );
}

function isChainNode(node: any): boolean {
  const props = node?.props;
  return (
    !!props?.overview &&
    (props.setSections !== undefined || props.onGameInfoButtonToggle !== undefined) &&
    !!node.type &&
    typeof node.type !== "string"
  );
}

function chainChildren(tree: any): any[] {
  const found: any[] = [];
  const walk = (node: any, depth: number) => {
    if (!node || depth > 50) return;
    if (Array.isArray(node)) return node.forEach((child) => walk(child, depth + 1));
    if (typeof node !== "object") return;
    if (isChainNode(node)) found.push(node);
    if (node.props?.children) walk(node.props.children, depth + 1);
  };
  walk(tree, 0);
  return found;
}

/** Insert the button after Play in a rendered ActionRow. Returns whether the row was found. */
function injectIntoRow(tree: any, overview: any): boolean {
  const rowClass = basicAppDetailsSectionStylerClasses?.ActionRow;
  if (!rowClass) return false;
  const row = findInReactTree(tree, (x: any) => typeof x?.props?.className === "string" && x.props.className.includes(rowClass));
  if (!row) return false;
  const children: any[] = Array.isArray(row.props.children) ? [...row.props.children] : [row.props.children];
  if (!isLaunchable(overview) || children.some((c) => c?.key === BUTTON_KEY)) return true;
  const playIndex = children.findIndex((c) => findInReactTree(c, (x: any) => x?.props?.bShowStreamingSelector !== undefined));
  children.splice(playIndex + 1, 0, <QuickscopeButton key={BUTTON_KEY} overview={overview} />);
  row.props.children = children;
  return true;
}

/** Patch a component node so `handler` sees its render output (same approach as @decky/ui's createReactTreePatcher). */
function patchComponent(node: any, prop: string, handler: Handler) {
  const type = node[prop];
  if (typeof type === "function" && !type.prototype?.render) {
    afterPatch(node, prop, handler);
  } else if (type?.prototype?.render) {
    wrapReactClass(node, prop);
    afterPatch(node[prop].prototype, "render", handler);
  } else if (typeof type === "object" && type) {
    wrapReactType(node, prop);
    patchComponent(node[prop], node[prop].render ? "render" : "type", handler);
  }
}

// Patched component types, per depth: original type -> patched copy.
const caches: Map<any, any>[] = [];

function follow(node: any, depth: number) {
  const cache = (caches[depth] ??= new Map());
  const patched = cache.get(node.type);
  if (patched) {
    node.type = patched;
    return;
  }
  const original = node.type;
  patchComponent(node, "type", (args, ret) => {
    const overview = args?.[0]?.overview;
    if (!overview || injectIntoRow(ret, overview)) return ret;
    if (depth < MAX_DEPTH) chainChildren(ret).forEach((child) => follow(child, depth + 1));
    return ret;
  });
  cache.set(original, node.type);
}

/** Add the game page patch. Returns a function that removes it. */
export function patchGamePage(): () => void {
  const patch = routerHook.addPatch(ROUTE, (tree: any) => {
    const routeProps = findInReactTree(tree, (x: any) => x?.renderFunc);
    if (routeProps) {
      afterPatch(routeProps, "renderFunc", (_: any[], ret: any) => {
        try {
          const page = findInReactTree(ret, (x: any) => x?.props?.overview && x?.props?.details);
          if (page?.type) follow(page, 0);
        } catch (e) {
          console.error("Quickscope: game page patch failed", e);
        }
        return ret;
      });
    }
    return tree;
  });
  return () => routerHook.removePatch(ROUTE, patch);
}
