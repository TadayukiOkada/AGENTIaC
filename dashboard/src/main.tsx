import { createRoot } from 'react-dom/client'
import './styles.css'
import { Dashboard } from './App'

document.documentElement.dataset.theme = 'light'
createRoot(document.getElementById('app')!).render(<Dashboard />)
