// Adds a "Quickscope" button to a game's library page, in the bottom-right
// corner of the header art, just above the Play row.
//
// It can't go inside the Play row itself: that row is rendered under a MobX
// observer class whose render is non-writable after its first call, so any
// patch below it is lost on the next re-render. The page component above it is
// a plain memo component and can be patched reliably, the same level other
// Decky plugins (e.g. ProtonDB badges) use. Every step is guarded: if Steam's
// layout changes, the button is simply not added.
import { routerHook } from "@decky/api";
import { DialogButton, afterPatch, appDetailsClasses, findInReactTree, wrapReactType } from "@decky/ui";
import { useState } from "react";
import { FaCrosshairs } from "react-icons/fa";

import { launch, toast } from "./launch";
import { isLaunchable, toLibraryApp } from "./library";

const ROUTE = "/library/app/:appid";
const BUTTON_KEY = "quickscope-launch";

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
  // Zero-height wrapper so the page layout doesn't move; the button floats
  // over the bottom edge of the header art, aligned with the Play row's right edge.
  return (
    <div style={{ position: "relative", height: 0, zIndex: 10 }}>
      <DialogButton
        disabled={busy}
        onClick={onClick}
        style={{
          position: "absolute",
          right: "36px",
          bottom: "12px",
          width: "auto",
          minWidth: 0,
          height: "40px",
          padding: "0 16px",
          display: "flex",
          alignItems: "center",
          gap: "8px",
          // No custom background: Steam's own button styles swap to a light
          // background with dark text on focus, and overriding the background
          // left dark text on dark.
        }}
      >
        <FaCrosshairs />
        Quickscope
      </DialogButton>
    </div>
  );
}

function injectButton(pageTree: any, overview: any) {
  if (!overview || !isLaunchable(overview)) return;
  const innerClass = appDetailsClasses?.InnerContainer;
  if (!innerClass) return;
  const inner = findInReactTree(
    pageTree,
    (x: any) => Array.isArray(x?.props?.children) && typeof x?.props?.className === "string" && x.props.className.includes(innerClass),
  );
  const children: any[] | undefined = inner?.props?.children;
  if (!children || children.some((c) => c?.key === BUTTON_KEY)) return;
  // Right after the header: the first child that isn't the overview panel.
  children.splice(1, 0, <QuickscopeButton key={BUTTON_KEY} overview={overview} />);
}

/** Add the game page patch. Returns a function that removes it. */
export function patchGamePage(): () => void {
  let patchedPageType: any = null;
  const patch = routerHook.addPatch(ROUTE, (tree: any) => {
    try {
      const routeProps = findInReactTree(tree, (x: any) => x?.renderFunc);
      if (!routeProps) return tree;
      afterPatch(routeProps, "renderFunc", (_: any[], ret: any) => {
        try {
          const page = findInReactTree(ret, (x: any) => x?.props?.overview && x?.props?.details);
          if (!page?.type) return ret;
          if (patchedPageType) {
            page.type = patchedPageType;
            return ret;
          }
          wrapReactType(page);
          afterPatch(page.type, "type", (args: any[], pageTree: any) => {
            try {
              injectButton(pageTree, args?.[0]?.overview);
            } catch (e) {
              console.error("Quickscope: game page button failed", e);
            }
            return pageTree;
          });
          patchedPageType = page.type;
        } catch (e) {
          console.error("Quickscope: game page patch failed", e);
        }
        return ret;
      });
    } catch (e) {
      console.error("Quickscope: game page route patch failed", e);
    }
    return tree;
  });
  return () => routerHook.removePatch(ROUTE, patch);
}
