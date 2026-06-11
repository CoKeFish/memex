// Colores y etiquetas por TIPO de vértice del grafo (`kind`), compartidos entre la vista `/grafo` y la
// cronología/story de un cúmulo. El color del vértice nativo «cúmulo» es el mismo en ambas.

export const CUMULO_COLOR = "#8b5cf6"

export const KIND_COLOR: Record<string, string> = {
  transaccion: "#10b981",
  evento: "#3b82f6",
  hackaton: "#a855f7",
  persona: "#14b8a6",
  organizacion: "#f97316",
  producto: "#f43f5e",
  registro: "#eab308",
  habito: "#ec4899",
  canal: "#0ea5e9",
  cumulo: CUMULO_COLOR,
}

export const KIND_LABEL: Record<string, string> = {
  transaccion: "Cobro/pago",
  evento: "Evento",
  hackaton: "Hackatón",
  persona: "Persona",
  organizacion: "Organización",
  producto: "Producto",
  registro: "Registro",
  habito: "Hábito",
  canal: "Canal",
  cumulo: "Cúmulo",
}

export const kindColor = (k: string): string => KIND_COLOR[k] ?? "#64748b"
