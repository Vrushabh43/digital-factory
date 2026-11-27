import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** when this value changes (e.g. the page address), the boundary tries to render again */
  resetKey?: unknown;
}

interface State {
  failed: boolean;
  key: unknown;
}

/** Shows a plain message instead of a blank page when a page fails to render. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false, key: this.props.resetKey };

  static getDerivedStateFromError(): Partial<State> {
    return { failed: true };
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.key ? { failed: false, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Page failed to render", error, info.componentStack);
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <main className="page">
        <div className="card section" role="alert">
          <h2>This page could not be shown</h2>
          <p className="small-note">
            Something unexpected went wrong while showing this page. The factory is not affected. Please reload the page or
            go back to the factory map.
          </p>
          <div className="actions-row">
            <button className="btn primary" onClick={() => window.location.reload()}>
              Reload page
            </button>
            <a className="btn" href="/">
              Go to the factory map
            </a>
          </div>
        </div>
      </main>
    );
  }
}
