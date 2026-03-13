import tkinter as tk

from stb_player import MediaPlayer


def run():
    root = tk.Tk()
    MediaPlayer(root)
    root.mainloop()


if __name__ == "__main__":
    run()
