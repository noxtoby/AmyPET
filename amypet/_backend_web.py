import logging
import shlex
import sys
from argparse import (
    SUPPRESS,
    _HelpAction,
    _StoreAction,
    _StoreTrueAction,
    _SubParsersAction,
    _VersionAction,
)
from os import fspath
from pathlib import Path, PurePath

import matplotlib.pyplot as plt
import mpld3
import streamlit as st
import streamlit.components.v1 as st_components
from matplotlib.figure import FigureBase
from packaging.version import Version
from streamlit.version import _get_installed_streamlit_version

from amypet.gui import BaseParser, __licence__, __version__, get_main_parser, patch_argument_kwargs

NONE = ''
PARSER = '==PARSER=='
log = logging.getLogger(__name__)
THIS = Path(__file__).parent
CONFIG = {
    'page_title': "AmyPET", 'page_icon': str(THIS / "program_icon.png"), 'layout': 'wide',
    'initial_sidebar_state': 'expanded'}
if _get_installed_streamlit_version() >= Version("0.88.1"):
    CONFIG['menu_items'] = {
        "Get help": "https://github.com/AMYPAD/AmyPET/issues", "Report a Bug": None, "About": f"""
AmyPET Pipeline

***version**: {__version__}

*GUI to run AmyPET tools* ([Source Code](https://github.com/AMYPAD/amypet)).

An https://amypad.eu Project.

{__licence__}"""}


class MyParser(BaseParser):
    def add_argument(self, *args, **kwargs):
        kwargs = patch_argument_kwargs(kwargs, gooey=True)
        widget = kwargs.pop('widget', None)
        widget_options = kwargs.pop('gooey_options', None)
        log.debug("%r, %r", args, kwargs)
        res = super(MyParser, self).add_argument(*args, **kwargs)
        if widget is not None:
            res.widget = widget
            res.widget_options = widget_options or {}
        return res


def st_output(res):
    if isinstance(res, dict) and '_amypet_imscroll' in res:
        data = res.pop('_amypet_imscroll')
        if isinstance(data, (str, PurePath)):
            st.image(fspath(data))
        else:
            if isinstance(data, FigureBase):
                fig = data
            else:
                fig = plt.figure()
                plt.imshow(data)
            mpld3.plugins.connect(fig, mpld3.plugins.MousePosition(fmt=".0f"))
            htm = mpld3.fig_to_html(fig)
            w, h = fig.get_size_inches()
            st_components.html(htm, width=int(w * 100) + 50, height=int(h * 100) + 50)
    return st.write(res)


def main():
    logging.basicConfig(level=logging.DEBUG)
    st.set_page_config(**CONFIG)
    parser = get_main_parser(gui_mode=False, argparser=MyParser)
    opts = {}

    def recurse(parser, key_prefix=""):
        opts[PARSER] = parser
        st.write(f"{'#' * (key_prefix.count('_') + 1)} {parser.prog.replace('-cli', '')}")

        for opt in parser._actions:
            if isinstance(opt, (_HelpAction, _VersionAction)) or opt.dest in {'dry_run'}:
                continue
            elif isinstance(opt, _StoreTrueAction):
                val = st.checkbox(opt.dest, value=opt.default, help=opt.help,
                                  key=f"{key_prefix}{opt.dest}")
                if val != opt.default:
                    opts[opt.dest] = val
            elif isinstance(opt, _StoreAction):
                dflt = NONE if opt.default is None else opt.default
                kwargs = {'help': opt.help, 'key': f"{key_prefix}{opt.dest}"}
                if hasattr(opt, 'widget'):
                    if opt.widget == "MultiFileChooser":
                        val = [
                            i.name for i in st.file_uploader(opt.dest, accept_multiple_files=True,
                                                             **kwargs)]
                    elif opt.widget == "FileChooser":
                        val = getattr(
                            st.file_uploader(opt.dest, accept_multiple_files=False, **kwargs),
                            'name', NONE)
                    elif opt.widget == "DirChooser":
                        # https://github.com/streamlit/streamlit/issues/1019
                        val = st.text_input(opt.dest, value=dflt, **kwargs)
                        if val.startswith(prefix := "file://"):
                            val = val[len(prefix):]
                    elif opt.widget == "IntegerField":
                        dflt = opt.default or 0
                        val = st.number_input(opt.dest, min_value=int(opt.widget_options['min']),
                                              max_value=int(opt.widget_options['max']), value=dflt,
                                              **kwargs)
                    elif opt.widget == "DecimalField":
                        dflt = opt.default or 0.0
                        val = st.number_input(opt.dest, min_value=float(opt.widget_options['min']),
                                              max_value=float(opt.widget_options['max']),
                                              format="%g",
                                              step=float(opt.widget_options['increment']),
                                              value=dflt, **kwargs)
                    else:
                        st.error(f"Unknown: {opt.widget}")
                        val = dflt
                elif opt.choices:
                    choices = list(opt.choices)
                    val = st.selectbox(opt.dest, index=choices.index(dflt), options=choices,
                                       **kwargs)
                else:
                    val = st.text_input(opt.dest, value=dflt, **kwargs)
                if val != dflt:
                    opts[opt.dest] = val
            elif isinstance(opt, _SubParsersAction):
                if opt.dest == SUPPRESS:
                    k = st.sidebar.radio(opt.help,
                                         options=sorted(set(opt.choices) - {'completion'}),
                                         key=f"{key_prefix}{opt.dest}")
                else:
                    k = st.sidebar.radio(opt.dest,
                                         options=sorted(set(opt.choices) - {'completion'}),
                                         **kwargs)
                recurse(opt.choices[k], f"{key_prefix}{k.replace('_', ' ')}_")
            else:
                st.warning(f"Unknown option type:{opt}")

    recurse(parser)
    st.sidebar.image(str(THIS / "config_icon.png"))

    parser = opts.pop(PARSER)
    left, right = st.columns([1, 2])
    with left:
        st.write("**Command**")
    with right:
        prefix = st.checkbox("Prefix")
    cmd = [Path(sys.executable).resolve().name, "-m", parser.prog] + [
        (f"--{k.replace('_', '-')}"
         if v is True else f"--{k.replace('_', '-')}={shlex.quote(str(v))}")
        for k, v in opts.items()]
    st.code(" ".join(cmd if prefix else cmd[2:]), "shell")
    dry_run = not st.button("Run")
    if dry_run:
        log.debug(opts)
    elif 'main__' in parser._defaults: # Cmd
        with st.spinner("Running"):
            st_output(parser._defaults['main__'](cmd[3:], verify_args=False))
    elif 'run__' in parser._defaults:  # Func
        with st.spinner("Running"):
            st_output(parser._defaults['run__'](**opts))
    else:
        st.error("Unknown action")


if __name__ == "__main__":
    main()
