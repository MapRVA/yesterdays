import Alpine from "alpinejs";

// Import our custom styles (includes Bootstrap via Sass)
import "./scss/styles.scss";

// Import Bootstrap JS
import * as bootstrap from "bootstrap";

// Import global component styles
import "./styles/main.css";
import "./styles/components/image-cards.css";
import "./styles/components/avatar-stack.css";

// Import global utilities
import "./js/components/notifications.js";
import { initRegionSelector } from "./js/components/region_selector";

// Import Font Awesome core
import { library, dom } from "@fortawesome/fontawesome-svg-core";

// Import only the specific icons we use (much smaller bundle)
import {
  faAlignLeft,
  faArchive,
  faArrowDown,
  faArrowLeft,
  faArrowRight,
  faArrowsAltH,
  faArrowsAltV,
  faArrowsLeftRight,
  faBan,
  faBook,
  faBookOpen,
  faCalendar,
  faCalendarDay,
  faCalendarDays,
  faChartBar,
  faChartLine,
  faChartPie,
  faCheck,
  faCheckCircle,
  faCheckDouble,
  faChevronDown,
  faChevronLeft,
  faChevronRight,
  faChevronUp,
  faCircleXmark,
  faClock,
  faCloudUploadAlt,
  faClone,
  faCode,
  faCog,
  faComment,
  faCompass,
  faCopy,
  faCrosshairs,
  faDownload,
  faDrawPolygon,
  faEdit,
  faEllipsisV,
  faExclamationCircle,
  faExclamationTriangle,
  faExpand,
  faExternalLinkAlt,
  faEye,
  faFilter,
  faFire,
  faFolder,
  faFolderOpen,
  faFont,
  faForward,
  faGift,
  faGlobe,
  faGripVertical,
  faHeading,
  faHeart,
  faHistory,
  faHome,
  faHourglass,
  faImage,
  faImages,
  faInbox,
  faInfoCircle,
  faKey,
  faLayerGroup,
  faList,
  faLock,
  faMagnifyingGlassPlus,
  faMap,
  faMapMarkedAlt,
  faMapMarkerAlt,
  faMapPin,
  faMinus,
  faMinusCircle,
  faPen,
  faPhotoVideo,
  faPlane,
  faPlay,
  faPlus,
  faQuestion,
  faRandom,
  faRedo,
  faRobot,
  faRocket,
  faSatellite,
  faSave,
  faSearch,
  faSearchLocation,
  faSeedling,
  faShieldAlt,
  faShuffle,
  faSignal,
  faSignInAlt,
  faSignOutAlt,
  faSpinner,
  faSquareCheck,
  faSquareUpRight,
  faStar,
  faSyncAlt,
  faTag,
  faTimes,
  faTrash,
  faUndo,
  faUpload,
  faUser,
  faUserCheck,
  faUserCircle,
  faUsers,
} from "@fortawesome/free-solid-svg-icons";

// Add all icons to the library
library.add(
  faAlignLeft,
  faArchive,
  faArrowDown,
  faArrowLeft,
  faArrowRight,
  faArrowsAltH,
  faArrowsAltV,
  faArrowsLeftRight,
  faBan,
  faBook,
  faBookOpen,
  faCalendar,
  faCalendarDay,
  faCalendarDays,
  faChartBar,
  faChartLine,
  faChartPie,
  faCheck,
  faCheckCircle,
  faCheckDouble,
  faChevronDown,
  faChevronLeft,
  faChevronRight,
  faChevronUp,
  faCircleXmark,
  faClock,
  faCloudUploadAlt,
  faClone,
  faCode,
  faCog,
  faComment,
  faCompass,
  faCopy,
  faCrosshairs,
  faDownload,
  faDrawPolygon,
  faEdit,
  faEllipsisV,
  faExclamationCircle,
  faExclamationTriangle,
  faExpand,
  faExternalLinkAlt,
  faEye,
  faFilter,
  faFire,
  faFolder,
  faFolderOpen,
  faFont,
  faForward,
  faGift,
  faGlobe,
  faGripVertical,
  faHeading,
  faHeart,
  faHistory,
  faHome,
  faHourglass,
  faImage,
  faImages,
  faInbox,
  faInfoCircle,
  faKey,
  faLayerGroup,
  faList,
  faLock,
  faMagnifyingGlassPlus,
  faMap,
  faMapMarkedAlt,
  faMapMarkerAlt,
  faMapPin,
  faMinus,
  faMinusCircle,
  faPen,
  faPhotoVideo,
  faPlane,
  faPlay,
  faPlus,
  faQuestion,
  faRandom,
  faRedo,
  faRobot,
  faRocket,
  faSatellite,
  faSave,
  faSearch,
  faSearchLocation,
  faSeedling,
  faShieldAlt,
  faShuffle,
  faSignal,
  faSignInAlt,
  faSignOutAlt,
  faSpinner,
  faSquareCheck,
  faSquareUpRight,
  faStar,
  faSyncAlt,
  faTag,
  faTimes,
  faTrash,
  faUndo,
  faUpload,
  faUser,
  faUserCheck,
  faUserCircle,
  faUsers,
);

// Replace any existing <i> tags with <svg> and set up a MutationObserver to
// continue doing this as the DOM changes.
dom.watch();

// Make Bootstrap globally available
window.bootstrap = bootstrap;

// Make Alpine globally available for page scripts to register components
window.Alpine = Alpine;

// Register the navbar region selector — must run after the window.Alpine
// assignment above and before Alpine.start() below.
initRegionSelector();

/**
 * Global CSRF token utility - available throughout the app
 */
window.getCsrfToken = function () {
  // First try to get from a form input
  const inputToken = document.querySelector(
    "[name=csrfmiddlewaretoken]",
  )?.value;
  if (inputToken) return inputToken;

  // Fall back to reading from cookie
  const name = "csrftoken";
  let cookieValue = null;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.substring(0, name.length + 1) === name + "=") {
        cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
        break;
      }
    }
  }
  return cookieValue;
};

// Start Alpine on DOMContentLoaded after all page scripts have registered their components.
// This ensures the load order is:
// 1. index.js loads and sets window.Alpine
// 2. Page-specific scripts load and call Alpine.data() to register components
// 3. DOMContentLoaded fires and Alpine.start() initializes all components
document.addEventListener("DOMContentLoaded", () => {
  Alpine.start();

  // Bootstrap tooltips are opt-in; enable them for server-rendered markup
  // that asks with data-bs-toggle="tooltip" (e.g. the avatar stack partial)
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((el) => {
    bootstrap.Tooltip.getOrCreateInstance(el);
  });
});

console.log("Vite bundle loaded with Alpine.js, Bootstrap, and Font Awesome");
