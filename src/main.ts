/**
 * main.ts
 *
 * Bootstraps Vuetify and other plugins then mounts the App.
 */

// Composables
import { createApp } from 'vue'

// Plugins
import { requestIsolation } from '@/lib/isolate'
import { registerPlugins } from '@/plugins'

// Components
import App from './App.vue'

// Styles
import 'unfonts.css'
import './styles/tailwind.css'
import './styles/main.scss'

await requestIsolation()

const app = createApp(App)

registerPlugins(app)

app.mount('#app')
