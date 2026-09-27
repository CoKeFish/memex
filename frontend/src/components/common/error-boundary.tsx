import { Component, type ErrorInfo, type ReactNode } from "react"
import { Panel } from "@/components/common/panel"
import { ErrorState } from "@/components/common/data-state"

// Red de seguridad de render. Sin esto, un throw en cualquier vista desmonta TODO el árbol de React y deja
// el dashboard en negro (así se manifestó el bug de stats anidadas en /pipeline). Atrapa el error, lo deja
// en consola para depurar y muestra un fallback acotado al área de contenido (el chrome sigue vivo).

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("ErrorBoundary atrapó un error de render:", error, info.componentStack)
  }

  reset = (): void => this.setState({ error: null })

  render(): ReactNode {
    if (this.state.error) {
      return (
        <Panel>
          <ErrorState
            title="Esta vista falló al renderizar"
            detail={this.state.error.message}
            onRetry={this.reset}
          />
        </Panel>
      )
    }
    return this.props.children
  }
}
